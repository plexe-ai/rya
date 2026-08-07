import { useEffect, useRef, useState } from 'react'
import { Globe, Mail, MessageCircle, MessagesSquare, Slack, Webhook } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { num } from '../lib/format'
import type { ConsoleState } from '../lib/types'
import { Ago, Empty, SecRow, Table, ViewHeader } from '../components/ui'

// ---- shapes -----------------------------------------------------------------
// Declared here rather than in lib/types.ts: only this view reads them, and the
// per-view response shapes are deliberately kept next to their single consumer.

/**
 * One row of `GET /agents/{agent}/sessions` — a session SUMMARY. `list_sessions`
 * strips `messages` server-side, which is why opening a row costs a second request
 * and why paging this list is cheap.
 */
export interface SessionSummary {
  id: string
  title?: string | null
  channel?: string | null
  externalId?: string | null
  status?: string | null
  messageCount?: number
  lastMessageAt?: string | null
  createdAt?: string | null
}

/**
 * `GET /agents/{agent}/sessions?limit=&offset=` — one page, newest activity first.
 *
 * `count` is the TOTAL number of conversations for the agent, **not** the length of
 * `sessions`: it is the N in "showing 50 of 137", and having it is the whole reason
 * this view asks the server rather than reading the `/console` aggregate's 50-row
 * preview. Optional because a runtime older than the paging contract answers
 * `{sessions: [...]}` and nothing else — see `total` below for what happens then.
 */
export interface SessionPage {
  sessions?: SessionSummary[]
  count?: number
  limit?: number
  offset?: number
}

/** One message of a transcript, oldest first. */
export interface SessionMessage {
  role?: string
  content?: string
  ts?: string
  runId?: string | null
}

/** `GET /sessions/{id}` — the summary plus the durable transcript. */
export interface SessionDetail extends SessionSummary {
  messages?: SessionMessage[]
}

const CHANNEL_ICON: Record<string, LucideIcon> = {
  slack: Slack,
  email: Mail,
  webhook: Webhook,
  web: Globe,
  sms: MessageCircle,
}

/**
 * Rows per page.
 *
 * 50 because that is exactly what the `/console` aggregate used to hand this view
 * (`snapshot.py`, `sessions[:50]`). The difference is that it is now a page size the
 * operator can spend rather than a ceiling nobody mentioned.
 */
const PAGE = 50

/**
 * The server's own ceiling: `limit` is clamped to 1..500. Asking for more is not
 * honoured, so "Load more" stops at it and says where the rest live instead of
 * offering a button that silently returns the same 500 rows for ever.
 */
const MAX_ROWS = 500

/**
 * Conversations — a paged session list plus an on-demand transcript.
 *
 * This view used to read `state.sessions` off the `/console` aggregate, and that one
 * shortcut was §5.2's root cause: the aggregate ships a **50-row preview** (it is a
 * dashboard field, not a list endpoint), the field was not even declared on
 * `ConsoleState`, and the view presented it as the whole list. A workspace with 137
 * conversations showed 50 with nothing saying so, and 51+ were unreachable from the
 * console at all. So the list is fetched here, agent-scoped and paged, and the count
 * line states what is on screen against what exists.
 *
 * Rows are keyed by session id, so a re-read that reorders the list (it is sorted by
 * last activity, so it reorders often) cannot move the selection or clobber the open
 * thread the way the legacy `innerHTML` re-render did.
 *
 * Note that an open transcript deliberately survives an agent switch being wrong:
 * views are unkeyed in `App.tsx` (§5.3), so the selection outliving the agent it
 * belongs to is fixed by keying the view there — not by a reset here that would be
 * dead code the moment it lands.
 */
export function ConversationsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  /**
   * Pages loaded, not pages accumulated: the fetch below always asks for
   * `offset=0&limit=pages*PAGE` and replaces the window.
   *
   * Merging successive pages client-side cannot be correct here, because the list is
   * sorted by last activity and page 2 is computed against an order that may have
   * changed since page 1 was read — a merge would drop conversations that moved up
   * into page 1 and show ones that moved down twice. Re-reading the whole window is
   * one request either way, and it is the same request the activity signature below
   * already issues, so the correct behaviour is also the cheaper one to write.
   */
  const [pages, setPages] = useState(1)
  const limit = Math.min(pages * PAGE, MAX_ROWS)

  /**
   * A change signature for "something happened in this agent's conversations".
   *
   * The list used to ride the shell's 6s poll, so it stayed live for free; owning the
   * fetch without this would have traded a truncated table for a stale one. Both of
   * these numbers are agent-scoped totals already in hand from that poll, and both
   * move on exactly the events that change this list — `sessions` when a session is
   * created, `messages` when a message is appended to any of them. So the loaded
   * window re-reads when the data actually changed and stays quiet when it did not,
   * which a second timer could not manage: a timer re-reads when nothing moved and
   * still lags when something did.
   */
  const activity = `${state.stats.sessions ?? 0}:${state.stats.messages ?? 0}`

  const { data, error, loading } = useLoad(
    // Agent-scoped, via `ag()`. The unprefixed `/sessions` resolves the reserved `_`
    // alias and 400s `E_AGENT_AMBIGUOUS` the moment a workspace serves a second
    // agent (lib/agent.ts), so the prefix is not a nicety.
    () => api<SessionPage>(ag(state.agent.name, `/sessions?limit=${limit}&offset=0`)),
    [state.agent.name, limit, activity],
  )

  const rows = data?.sessions ?? []

  /**
   * The N in "showing X of N".
   *
   * `count` from the page when the server sends it. `state.stats.sessions` is the
   * fallback and not a guess: `snapshot.py` computes it as `len(list_sessions(agent))`
   * over the same store call this endpoint pages, so a runtime that predates the
   * paging contract still gets an honest denominator. `Math.max` against what is on
   * screen because a denominator smaller than the numerator ("showing 50 of 12", from
   * a `stats` reading one poll older than the list) would read as a bug in the console
   * rather than as the race it is.
   */
  const total = Math.max(data?.count ?? state.stats.sessions ?? 0, rows.length)

  /** The conversation the operator asked for. `thread` is what is loaded FOR it. */
  const [openId, setOpenId] = useState<string | null>(null)
  const [thread, setThread] = useState<{ id: string; detail: SessionDetail } | null>(null)
  const [loadingId, setLoadingId] = useState<string | null>(null)
  const [threadError, setThreadError] = useState<string | null>(null)
  /** Bumped on every click, so re-clicking the open row is a retry (see below). */
  const [attempt, setAttempt] = useState(0)

  /**
   * §5.2, defect 3: the rendered transcript is DERIVED against the selection.
   *
   * The old view rendered `thread` unconditionally, and only showed its loader when
   * nothing was open at all — so clicking a second row left the first row's messages
   * on screen, under the new row's selection, with no indication anything was loading.
   * Wrong data presented as current is the one outcome this console must not produce,
   * and no amount of careful `setThread` sequencing rules it out; this does, because a
   * transcript that does not belong to the selected id cannot reach the DOM. A moment
   * of empty space is the correct trade. The id compared is the one we REQUESTED, not
   * the one the response echoed, so a server that omits or rewrites `id` degrades to
   * "still loading" rather than to a blank panel.
   */
  const shown = thread && thread.id === openId ? thread.detail : null
  const openRow = openId ? rows.find((s) => s.id === openId) : undefined

  /**
   * What the LIST says about the open conversation, as a value an effect can key on.
   *
   * §5.2, defect 2: the open transcript was loaded once on click and never re-read, so
   * new messages in the conversation an operator was watching never appeared, however
   * long they watched. Both fields move when a message is appended, so this changes
   * exactly when there is something new to fetch — precise, and free while the
   * conversation is idle. Empty when the open session is outside the loaded window,
   * which is a value like any other: it settles and stops firing.
   */
  const openSig = openRow ? `${openRow.lastMessageAt ?? ''}:${openRow.messageCount ?? 0}` : ''

  // Monotonic request token. Clicking a second conversation while the first is
  // still in flight must not let the slower response win: without this, the
  // transcript on screen can end up belonging to a row nobody selected.
  const seq = useRef(0)

  // `onToast` is a fresh closure on every one of the shell's 6s polls, so it is held
  // in a ref and kept out of the effect's dependencies — as a dependency it would
  // re-fetch the transcript six times a minute for no reason.
  const onToastRef = useRef(onToast)
  onToastRef.current = onToast

  useEffect(() => {
    if (!openId) return
    const mine = ++seq.current
    setLoadingId(openId)
    setThreadError(null)
    void (async () => {
      try {
        // Workspace-scoped, NOT agent-prefixed: the session id already identifies
        // the agent, and the server only serves `/sessions/{id}` unprefixed.
        const detail = await api<SessionDetail>(`/sessions/${encodeURIComponent(openId)}`)
        if (mine !== seq.current) return
        setThread({ id: openId, detail })
      } catch (e) {
        if (mine !== seq.current) return
        const msg = e instanceof Error ? e.message : String(e)
        setThreadError(msg)
        onToastRef.current(`Conversation error — ${msg}`)
      } finally {
        if (mine === seq.current) setLoadingId(null)
      }
    })()
    // `attempt` is in here so that re-clicking the row already open re-reads it: it
    // is the only retry an operator has after a failed transcript, and without it a
    // click that changes neither the id nor the activity would do nothing at all.
    // `onToast` is deliberately absent — see the ref above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId, openSig, attempt])

  function closeThread() {
    // The in-flight request is orphaned rather than merely ignored on arrival: the
    // token bump is what stops a response landing after the panel was dismissed and
    // re-opening it under the operator.
    seq.current++
    setOpenId(null)
    setThread(null)
    setLoadingId(null)
    setThreadError(null)
  }

  const messages = shown?.messages ?? []
  /** The list row while the transcript loads, so the header is honest immediately. */
  const head = shown ?? openRow

  return (
    <>
      <ViewHeader title="Conversations">
        Durable, threaded sessions. Inbound events are grouped by identity, so an agent
        remembers a conversation across processes and restarts.
      </ViewHeader>

      {error && rows.length === 0 ? (
        // A failed read is not an empty workspace, and must never be reported as one:
        // "No conversations yet" over an outage is the outage-vs-idle confusion this
        // console exists to prevent. An unauthenticated console is a third state again
        // — see `lib/api.ts`, where only a 401 the stored credential explains carries
        // the `'unauthorized'` sentinel.
        <Empty icon={MessagesSquare}>
          {error === 'unauthorized'
            ? 'Connect to load conversations.'
            : `Conversations unavailable — ${error}`}
        </Empty>
      ) : (
        <Table
          rows={rows}
          rowKey={(s) => s.id}
          onRowClick={(s) => {
            setOpenId(s.id)
            setAttempt((n) => n + 1)
          }}
          rowLabel={(s) => `Open conversation ${s.title || s.id}`}
          emptyIcon={MessagesSquare}
          emptyMessage={
            // Three states, not two. An empty list is an ordinary state on a fresh
            // install — sessions appear the first time something sends the agent a
            // message — but a first read still in flight is not that, and saying so
            // would be a claim we have not verified yet.
            loading
              ? 'Loading conversations…'
              : 'No conversations yet — inbound events create sessions automatically.'
          }
          columns={[
            {
              header: 'Conversation',
              cell: (s) => {
                const Icon = CHANNEL_ICON[s.channel ?? ''] ?? MessagesSquare
                return (
                  <>
                    <Icon
                      aria-hidden="true"
                      focusable="false"
                      style={{
                        width: 14,
                        height: 14,
                        verticalAlign: -2,
                        marginRight: 7,
                        color: 'var(--text-3)',
                      }}
                    />
                    {s.title || 'Conversation'}
                  </>
                )
              },
            },
            { header: 'Channel', cell: (s) => <span className="mono">{s.channel || '—'}</span> },
            { header: 'Thread', cell: (s) => <span className="mono dim">{s.externalId || '—'}</span> },
            { header: 'Messages', cell: (s) => String(s.messageCount ?? 0) },
            { header: 'Last activity', cell: (s) => <Ago ts={s.lastMessageAt} />, className: 'dim' },
          ]}
        />
      )}

      {rows.length > 0 && (
        /*
          §5.2, defect 1: the truncation is STATED, and the rest is reachable.
          An operator who cannot tell a complete list from a truncated one cannot
          trust either, and the old view gave them no way to tell — nor any way to
          reach conversation 51.
        */
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 12 }}>
          <span className="sub" style={{ marginBottom: 0 }}>
            Showing {num(rows.length)} of {num(total)} conversation{total === 1 ? '' : 's'}
            {loading ? ' · reading…' : ''}
          </span>
          {rows.length < total &&
            (limit < MAX_ROWS ? (
              <button
                className="btn sm"
                // Disabled only while our own read is in flight, so an impatient
                // double-click asks for 100 rows and not 250.
                disabled={loading}
                onClick={() => setPages((p) => p + 1)}
              >
                Load more
              </button>
            ) : (
              <span className="sub" style={{ marginBottom: 0 }}>
                {num(MAX_ROWS)} is the server's per-request ceiling — read older
                conversations with{' '}
                <span className="mono">
                  GET /agents/{state.agent.name}/sessions?offset={MAX_ROWS}
                </span>
                .
              </span>
            ))}
        </div>
      )}

      {error && rows.length > 0 && (
        // Stale-but-real beats blank (usePoll's rule: don't clobber a live view), so
        // the last good window stays on screen — and says that it is the last good
        // window, because silently ageing data is how an operator ends up acting on it.
        <div className="sub" style={{ marginTop: 12 }}>
          {error === 'unauthorized'
            ? 'This list is the last good read — connect again to keep it current.'
            : `This list is the last good read (${error}); conversations newer than it are missing from the table above.`}
        </div>
      )}

      {openId && (
        <>
          <SecRow
            left={
              <>
                {head?.title || 'Conversation'} · <span className="mono">{head?.channel || ''}</span>
              </>
            }
            right={
              <>
                <span className="mono">
                  {/* Never "reading…" over a read that already failed — the panel
                      below says what happened, and this must not contradict it. */}
                  {shown ? `${messages.length} messages` : threadError ? 'unavailable' : 'reading…'}
                  {head?.externalId ? ` · ${head.externalId}` : ''}
                  {/* A re-read of the OPEN conversation annotates rather than blanks:
                      nothing on screen is wrong, it is merely about to grow. */}
                  {shown && loadingId === openId ? ' · refreshing' : ''}
                </span>{' '}
                {/* §5.2, defect 4: an opened transcript could not be dismissed at all. */}
                <button className="btn sm" aria-label="Close conversation" onClick={closeThread}>
                  Close
                </button>
              </>
            }
          />
          {shown ? (
            <div className="window" style={{ padding: '14px 16px' }}>
              {messages.length === 0 ? (
                // An existing session with no messages is a real answer, and a
                // different one from "no such session" (which arrives as a 404 and
                // becomes both a toast and the failure line below).
                <Empty>No messages in this conversation.</Empty>
              ) : (
                <div className="thread">
                  {messages.map((m, i) => {
                    const role = (m.role ?? '').toLowerCase()
                    const mine = role === 'assistant' || role === 'agent'
                    const sys = role === 'system' || role === 'tool'
                    return (
                      // Messages carry no id, so the key pairs the timestamp with the
                      // position. The transcript is append-only and never re-sorted,
                      // so this stays stable across a re-read.
                      <div
                        className={`msg ${mine ? 'out' : 'in'}${sys ? ' sys' : ''}`}
                        key={`${m.ts ?? ''}-${i}`}
                      >
                        <div className="mrole">
                          {m.role}
                          {m.runId ? (
                            <>
                              {' · '}
                              <span className="mono">{m.runId}</span>
                            </>
                          ) : null}
                        </div>
                        <div className="mbody">{m.content}</div>
                        <div className="mts">{(m.ts ?? '').slice(11, 19)}</div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          ) : threadError ? (
            <Empty>
              {threadError === 'unauthorized'
                ? 'Connect to read this transcript.'
                : `Transcript unavailable — ${threadError}. Click the row again to retry.`}
            </Empty>
          ) : (
            // The default with a selection and nothing rendered: a read is in flight.
            // Shown for the PENDING id whether or not something was already open,
            // which is the visible half of defect 3.
            <Empty>Reading transcript…</Empty>
          )}
          {shown && threadError && (
            // A refresh failed over a transcript that had already loaded. The messages
            // stay — they are real — but an operator watching a live conversation has
            // to know the tail stopped arriving.
            <div className="sub" style={{ marginTop: 12 }}>
              {threadError === 'unauthorized'
                ? 'This transcript is the last good read — connect again to keep following it.'
                : `This transcript is the last good read (${threadError}); newer messages are missing from it.`}
            </div>
          )}
        </>
      )}
    </>
  )
}
