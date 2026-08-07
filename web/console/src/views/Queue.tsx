import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle, Check, CheckCircle2, Circle, FileText, Flag, Inbox, ListOrdered, LoaderCircle,
  Play, Plug, Send, ShieldAlert, Sparkles, XCircle,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { API, api, getToken } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { num } from '../lib/format'
import type { ConsoleState, QueueCounts } from '../lib/types'
import { CopyId, Empty, Mono, SecRow, StatusBadge, Table, Tile, ViewHeader } from '../components/ui'

// ---- shapes -----------------------------------------------------------------
// Local to this view on purpose: `/queue/*` and the turn stream have exactly one
// consumer, so their shapes live beside it rather than in lib/types.ts.

/** `GET /queue/jobs` — one durable job. `chat-turn` jobs also have a turn stream. */
export interface QueueJob {
  id: string
  type: string
  status: string
  attempts?: number
  maxAttempts?: number
  workerId?: string | null
  error?: string | null
  lastError?: string | null
  deadLetter?: boolean
}

export interface QueueStats {
  counts?: QueueCounts
}

export interface QueueJobs {
  jobs?: QueueJob[]
}

/** The kinds `_tail_turn` appends to a turn's durable buffer (`turns.py`). */
export type FrameKind = 'token' | 'trace' | 'ui' | 'message' | 'run' | 'error' | 'restart' | (string & {})

/**
 * One durable frame. `seq` is the whole point: it is the cursor that makes the
 * stream RESUMABLE, so it is kept even though nothing renders it directly.
 */
export interface TurnFrame {
  seq: number
  kind: FrameKind
  data: Record<string, unknown> | null
}

/**
 * A run frame is terminal for these statuses only.
 *
 * `waiting_approval` is deliberately absent: it is a PAUSE marker, not an ending.
 * The approval resolution appends the continuation and the real terminal frame to
 * the SAME buffer, and `/turns/{id}/stream` is the resumable transport that keeps
 * tailing across the pause (`app.py: _tail_turn`, `stop_on_pause=False` here).
 * Treating it as terminal would report a paused turn as finished.
 */
const TERMINAL_RUN_STATUSES = ['completed', 'failed', 'rejected', 'needs_reconnect']

function isTerminal(f: TurnFrame): boolean {
  if (f.kind === 'error') return true
  return f.kind === 'run' && TERMINAL_RUN_STATUSES.includes(String(f.data?.status ?? ''))
}

const TRACE_ICON: Record<string, LucideIcon> = {
  'run.started': Play,
  'tool.call': Plug,
  'llm.respond': Sparkles,
  'approval.requested': ShieldAlert,
  'approval.approved': Check,
  'approval.rejected': XCircle,
  'channel.send': Send,
  'run.completed': CheckCircle2,
  'run.failed': XCircle,
  log: FileText,
}

// ---- SSE parsing ------------------------------------------------------------

/** What one decoded chunk yielded: data frames, and comment lines. */
interface Parsed {
  frames: TurnFrame[]
  /** True when the server sent its idle notice — the invitation to reconnect. */
  idle: boolean
}

/**
 * Incremental SSE parser over the raw byte stream.
 *
 * Hand-rolled because the request needs an `Authorization` header and
 * `EventSource` cannot send one — the same reason the legacy console used
 * `fetch` + `getReader()`. Frames are split on blank lines and may straddle a
 * chunk boundary, so the leftover buffer is closure state.
 *
 * Lines beginning with `:` are SSE COMMENTS, never data. The server sends two:
 * `: keep-alive` (defeats proxy idle timeouts) and `: idle-timeout` (nothing has
 * been appended for `RYA_TURN_STREAM_IDLE_SECONDS`, so the connection is about to
 * close and the client should reconnect from its cursor rather than hang). The
 * legacy reader skipped both alike; distinguishing them is what makes the idle
 * close a resume instead of a silent stall.
 */
function sseParser() {
  let buf = ''
  let seq: number | null = null
  let kind: string | null = null
  let data: string[] = []

  return function feed(text: string): Parsed {
    const out: Parsed = { frames: [], idle: false }
    buf += text
    let nl: number
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).replace(/\r$/, '')
      buf = buf.slice(nl + 1)
      if (line.startsWith(':')) {
        if (line.includes('idle')) out.idle = true
        continue
      }
      if (line.startsWith('id:')) {
        const n = Number(line.slice(3).trim())
        if (Number.isFinite(n)) seq = n
      } else if (line.startsWith('event:')) {
        kind = line.slice(6).trim()
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).trim())
      } else if (line === '' && kind) {
        let parsed: Record<string, unknown> | null = null
        try {
          parsed = data.length ? (JSON.parse(data.join('\n')) as Record<string, unknown>) : null
        } catch {
          // A frame the server could not serialize cleanly is still a frame; keep
          // its kind and drop the payload rather than abandoning the whole tail.
        }
        out.frames.push({ seq: seq ?? -1, kind, data: parsed })
        seq = null
        kind = null
        data = []
      }
    }
    return out
  }
}

// ---- the durable turn-stream inspector --------------------------------------

type StreamStatus = 'tailing' | 'done' | 'stalled' | 'error'

/** How many consecutive reconnects may return no new frames before giving up. */
const MAX_EMPTY_RECONNECTS = 3
/** Backoff after an UNEXPECTED close. An idle notice reconnects immediately. */
const RECONNECT_MS = 800

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      clearTimeout(t)
      resolve()
    })
  })
}

/**
 * Tail one turn's durable buffer.
 *
 * The durability story rendered: frames are appended to a store-backed, SEQUENCED
 * buffer, so this is a tail over history rather than a live wire. A finished turn
 * flushes instantly, a crashed executor's reclaim shows up as a `restart` frame,
 * and a dropped connection resumes with `?after=<lastSeq>` instead of losing the
 * run. The legacy console read the same endpoint but gave up after a 6s timer and
 * never sent a cursor, so a turn longer than six seconds looked truncated.
 *
 * Two things React forces us to get right that a one-shot `innerHTML` render did
 * not have to think about:
 *
 *  1. The effect keys on `[agent, turnId]` and nothing else, so the shell's 6s
 *     poll — which hands the parent a brand-new `state` object every time — cannot
 *     restart the stream. `onToast` is a fresh closure on each of those renders,
 *     so it is held in a ref and kept OUT of the dependency list.
 *  2. The destructor aborts. A leaked reader keeps decoding into a component that
 *     no longer exists, and in a poll-driven shell one leak per turn switch
 *     compounds into many open connections against the same buffer.
 */
function TurnStream({
  agent,
  turnId,
  onToast,
}: {
  agent: string
  turnId: string
  onToast: (m: string) => void
}) {
  const [frames, setFrames] = useState<TurnFrame[]>([])
  const [status, setStatus] = useState<StreamStatus>('tailing')
  const [resumes, setResumes] = useState(0)

  const onToastRef = useRef(onToast)
  onToastRef.current = onToast

  useEffect(() => {
    const ctrl = new AbortController()
    const { signal } = ctrl
    setFrames([])
    setStatus('tailing')
    setResumes(0)

    /** Last seq seen. `-1` is the server's "from the beginning" sentinel. */
    let cursor = -1

    /** One connection. Returns why it ended. */
    async function readOnce(): Promise<{ terminal: boolean; idle: boolean }> {
      // Agent-scoped: `/agents/{name}/turns/{id}/stream`. The unprefixed spelling
      // resolves the reserved `_` alias and 400s `E_AGENT_AMBIGUOUS` the moment a
      // workspace serves a second agent, so `ag()` is not a nicety here.
      const path = ag(agent, `/turns/${encodeURIComponent(turnId)}/stream`)
      // Omitted on the first connect so the server applies its own `after=-1`.
      const url = `${API}${path}${cursor >= 0 ? `?after=${cursor}` : ''}`
      const token = getToken()
      const r = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal,
      })
      if (r.status === 401) throw new Error('unauthorized')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      if (!r.body) return { terminal: false, idle: false }

      const reader = r.body.getReader()
      // An in-flight `read()` does not settle on its own when the signal trips
      // (and with a stubbed body nothing rejects it at all), so cancel explicitly.
      const release = () => void reader.cancel().catch(() => {})
      signal.addEventListener('abort', release)
      const dec = new TextDecoder()
      const feed = sseParser()
      let terminal = false
      let idle = false
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done || signal.aborted) break
          const { frames: batch, idle: sawIdle } = feed(dec.decode(value, { stream: true }))
          if (sawIdle) idle = true
          if (batch.length) {
            for (const f of batch) {
              if (f.seq > cursor) cursor = f.seq
              if (isTerminal(f)) terminal = true
            }
            if (!signal.aborted) setFrames((prev) => [...prev, ...batch])
          }
          if (terminal || idle) break
        }
      } finally {
        signal.removeEventListener('abort', release)
        release()
      }
      return { terminal, idle }
    }

    void (async () => {
      let empties = 0
      for (;;) {
        const before = cursor
        let outcome: { terminal: boolean; idle: boolean }
        try {
          outcome = await readOnce()
        } catch (e) {
          if (signal.aborted) return
          setStatus('error')
          onToastRef.current(
            e instanceof Error && e.message === 'unauthorized'
              ? 'Connect to read the turn stream.'
              : `Turn stream error — ${e instanceof Error ? e.message : String(e)}`,
          )
          return
        }
        if (signal.aborted) return
        if (outcome.terminal) {
          setStatus('done')
          return
        }
        // Progress refills the reconnect budget: a long turn may idle out many
        // times legitimately, but a server that closes instantly and appends
        // nothing must not be retried forever.
        if (cursor > before) empties = 0
        else if (++empties > MAX_EMPTY_RECONNECTS) {
          setStatus('stalled')
          return
        }
        setResumes((n) => n + 1)
        // An idle notice is the server's own invitation to reconnect, so take it
        // at once; any other close was a drop and gets a short backoff.
        if (!outcome.idle) await delay(RECONNECT_MS, signal)
        if (signal.aborted) return
      }
    })()

    return () => ctrl.abort()
    // onToast is intentionally absent — see the note above about the 6s poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, turnId])

  const blocks = groupFrames(frames)

  return (
    <>
      <SecRow
        left={
          <>
            Turn stream · <Mono>{turnId}</Mono>
          </>
        }
        right={
          <span className="mono">
            {frames.length} frames · durable buffer
            {resumes > 0 ? ` · resumed ${resumes}×` : ''}
            {status === 'tailing' ? ' · tailing…' : ''}
            {status === 'stalled' ? ' · idle' : ''}
          </span>
        }
      />
      {blocks.length === 0 ? (
        <Empty>
          {status === 'tailing'
            ? 'Reading durable buffer…'
            : 'No frames yet — the turn has not executed.'}
        </Empty>
      ) : (
        <div className="window" style={{ padding: '10px 16px' }}>
          <div className="trace">
            {blocks.map((b) => (
              <StreamBlock block={b} key={b.key} />
            ))}
          </div>
        </div>
      )}
    </>
  )
}

// ---- frames -> renderable blocks --------------------------------------------

type Block =
  | { key: string; type: 'text'; text: string }
  | { key: string; type: 'trace'; kind: string; label: string }
  | { key: string; type: 'message'; role: string; content: string }
  | { key: string; type: 'restart'; attempt: string }
  | { key: string; type: 'run'; status: string; runId: string; tokens: number }
  | { key: string; type: 'error'; message: string }

/**
 * Coalesce the frame list into what an operator should read.
 *
 * `token` frames are one LLM chunk each — hundreds per turn — so consecutive ones
 * fold into a single bubble, exactly like the legacy `flushTok()`. Keys come from
 * the frame `seq` (the first token's seq for a folded run), so they are real ids
 * rather than array positions and survive frames being appended.
 *
 * Pure, and exported so the mapping can be tested without a stream.
 */
export function groupFrames(frames: TurnFrame[]): Block[] {
  const out: Block[] = []
  let text = ''
  let textKey = ''
  const flush = () => {
    if (text) out.push({ key: `text-${textKey}`, type: 'text', text })
    text = ''
    textKey = ''
  }
  for (const f of frames) {
    const d = f.data ?? {}
    if (f.kind === 'token') {
      if (!textKey) textKey = String(f.seq)
      text += typeof d.text === 'string' ? d.text : ''
      continue
    }
    flush()
    const key = `${f.kind}-${f.seq}`
    if (f.kind === 'trace') {
      out.push({
        key,
        type: 'trace',
        kind: String(d.kind ?? 'trace'),
        label: typeof d.label === 'string' ? d.label : '',
      })
    } else if (f.kind === 'message') {
      out.push({
        key,
        type: 'message',
        role: typeof d.role === 'string' ? d.role : 'assistant',
        content: typeof d.content === 'string' ? d.content : '',
      })
    } else if (f.kind === 'restart') {
      out.push({ key, type: 'restart', attempt: String(d.attempt ?? '?') })
    } else if (f.kind === 'run') {
      out.push({
        key,
        type: 'run',
        status: String(d.status ?? '?'),
        runId: String(d.id ?? ''),
        tokens: typeof d.tokens === 'number' ? d.tokens : 0,
      })
    } else if (f.kind === 'error') {
      out.push({ key, type: 'error', message: typeof d.message === 'string' ? d.message : '' })
    }
    // `ui` frames are skipped, as in the legacy inspector: they are chat-surface
    // directives (cards, pickers) meant for the client that started the turn, not
    // operator-facing evidence.
  }
  flush()
  return out
}

function StreamBlock({ block }: { block: Block }) {
  if (block.type === 'text') {
    return (
      <div className="msg out" style={{ maxWidth: '100%' }}>
        <div className="mrole">streamed text</div>
        <div className="mbody">{block.text}</div>
      </div>
    )
  }
  if (block.type === 'message') {
    return (
      <div className="msg out" style={{ maxWidth: '100%' }}>
        <div className="mrole">{block.role}</div>
        <div className="mbody">{block.content}</div>
      </div>
    )
  }
  if (block.type === 'restart') {
    // Amber and centred, because this is the durability guarantee firing: a lease
    // expired, a worker reclaimed the turn, and the run continued.
    return (
      <div className="msg sys" style={{ maxWidth: '100%' }}>
        <div className="mbody">
          stream restarted — reclaimed after a crash (attempt {block.attempt})
        </div>
      </div>
    )
  }
  if (block.type === 'trace') {
    const Icon = TRACE_ICON[block.kind] ?? Circle
    const amber = block.kind.startsWith('approval')
    return (
      <div className="tev">
        <span className={`tdot${amber ? ' amber' : ''}`}>
          <Icon aria-hidden="true" focusable="false" />
        </span>
        <div>
          <div className="tk">{block.kind}</div>
          <div className="tm">{block.label}</div>
        </div>
      </div>
    )
  }
  if (block.type === 'run') {
    const Icon =
      block.status === 'completed'
        ? CheckCircle2
        : block.status === 'waiting_approval'
          ? ShieldAlert
          : Flag
    return (
      <div className="tev">
        <span className={`tdot${block.status === 'waiting_approval' ? ' amber' : ''}`}>
          <Icon aria-hidden="true" focusable="false" />
        </span>
        <div>
          <div className="tk">run · {block.status}</div>
          <div className="tm mono">
            {block.runId} · {num(block.tokens)} tokens
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className="tev">
      <span className="tdot">
        <XCircle aria-hidden="true" focusable="false" />
      </span>
      <div>
        <div className="tk">error</div>
        <div className="tm">{block.message}</div>
      </div>
    </div>
  )
}

// ---- the view ---------------------------------------------------------------

/**
 * Queue & turns — the durable job table plus the turn-stream inspector.
 *
 * Loads on ENTRY rather than from the shell's 6s poll (console/AGENTS.md): two
 * requests for a table an operator opens deliberately, and re-fetching it every
 * six seconds would also fight the open stream below for the connection budget.
 * `reload()` runs after a retry/cancel, because those are the moments the table
 * actually changed.
 */
export function QueueView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const { data, error, loading, reload } = useLoad(async () => {
    // Both workspace-scoped: the queue is a per-workspace resource and the server
    // serves no agent-prefixed spelling of `/queue/*`.
    const [stats, jobs] = await Promise.all([
      api<QueueStats>('/queue/stats'),
      api<QueueJobs>('/queue/jobs'),
    ])
    return { counts: stats.counts ?? {}, jobs: jobs.jobs ?? [] }
  })

  // Per-job, not one flag: cancelling one job must not disable every other row.
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [turnId, setTurnId] = useState<string | null>(null)

  async function act(job: QueueJob, action: 'retry' | 'cancel') {
    setBusy((b) => ({ ...b, [job.id]: true }))
    try {
      await api(`/queue/jobs/${encodeURIComponent(job.id)}/${action}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{}',
      })
      onToast(action === 'retry' ? `Requeued ${job.id}` : `Cancel requested for ${job.id}`)
      await reload()
    } catch (e) {
      onToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      // Cleared on both paths: on success the row comes back with a new status,
      // on failure the operator needs the button back.
      setBusy((b) => {
        const { [job.id]: _drop, ...rest } = b
        return rest
      })
    }
  }

  const counts: QueueCounts = data?.counts ?? {}
  const jobs = data?.jobs ?? []

  return (
    <>
      <ViewHeader title="Queue &amp; turns">
        Durable work with leases, bounded retries and a dead-letter queue. Chat turns are
        queued the same way, so a crashed worker resumes a conversation instead of losing it.
      </ViewHeader>

      <div className="stats">
        <Tile
          icon={Inbox}
          label="Pending"
          value={num(counts.pending)}
          sub="awaiting a worker"
          amber={(counts.pending ?? 0) > 0}
        />
        <Tile icon={LoaderCircle} label="Running" value={num(counts.running)} sub="leased to workers" />
        <Tile icon={CheckCircle2} label="Completed" value={num(counts.completed)} sub="done" />
        <Tile
          icon={AlertTriangle}
          label="Dead-letter"
          value={num(counts.failed)}
          sub="attempts exhausted"
          amber={(counts.failed ?? 0) > 0}
        />
      </div>

      {error ? (
        // An unauthenticated console is not an outage; say which one this is.
        <Empty icon={ListOrdered}>
          {error === 'unauthorized' ? 'Connect to load the queue.' : `Queue unavailable — ${error}`}
        </Empty>
      ) : (
        <Table
          rows={jobs}
          rowKey={(j) => j.id}
          emptyIcon={ListOrdered}
          // An empty queue is the NORMAL state of a healthy deployment — work is
          // claimed within milliseconds — so this must never read as a fault.
          emptyMessage={
            loading ? 'Loading queue…' : 'Queue is empty — enqueue with POST /queue/jobs or start a durable turn.'
          }
          columns={[
            {
              header: 'Job',
              cell: (j) => (
                <>
                  {/* Job ids are copied into `rya` commands and support threads. */}
                  <CopyId id={j.id} onCopied={onToast} />
                  {j.deadLetter ? <> <span className="pb dis">DLQ</span></> : null}
                </>
              ),
            },
            {
              header: 'Type',
              cell: (j) => (
                <>
                  <Mono>{j.type}</Mono>
                  {j.type === 'chat-turn' ? <> <span className="ptag">turn</span></> : null}
                </>
              ),
            },
            { header: 'Status', cell: (j) => <StatusBadge status={j.status} /> },
            { header: 'Attempts', cell: (j) => `${j.attempts ?? 0}/${j.maxAttempts ?? 1}` },
            {
              header: 'Worker',
              cell: (j) => <Mono>{j.workerId || '—'}</Mono>,
              className: 'dim',
            },
            {
              header: 'Error',
              cell: (j) => <span title={j.error || j.lastError || ''}>{j.error || j.lastError || '—'}</span>,
              className: 'dim',
            },
            {
              header: '',
              cell: (j) => (
                <>
                  {/* Only `chat-turn` jobs have a durable frame buffer, so the
                      inspector is a per-row button rather than a whole-table
                      row click: `Table` marks every row `clickable`, and an
                      affordance on a row with nothing to open is a lie. */}
                  {j.type === 'chat-turn' && (
                    <button
                      className="btn sm"
                      onClick={() => setTurnId(j.id)}
                      aria-label={`Inspect turn stream ${j.id}`}
                    >
                      Inspect
                    </button>
                  )}{' '}
                  {(j.status === 'failed' || j.status === 'cancelled') && (
                    <button className="btn sm" onClick={() => void act(j, 'retry')} disabled={!!busy[j.id]}>
                      Retry
                    </button>
                  )}{' '}
                  {(j.status === 'pending' || j.status === 'running') && (
                    <button className="btn sm" onClick={() => void act(j, 'cancel')} disabled={!!busy[j.id]}>
                      Cancel
                    </button>
                  )}
                </>
              ),
            },
          ]}
        />
      )}

      {turnId && (
        // Keyed by turn id so selecting another turn REMOUNTS the inspector: the
        // frame list, the cursor and the AbortController all belong to one turn,
        // and remounting disposes of the previous reader instead of interleaving
        // two buffers into one list.
        <TurnStream key={turnId} agent={state.agent.name} turnId={turnId} onToast={onToast} />
      )}
    </>
  )
}
