import { useRef, useState } from 'react'
import { Globe, Mail, MessageCircle, MessagesSquare, Slack, Webhook } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../lib/api'
import type { ConsoleState } from '../lib/types'
import { Ago, Empty, SecRow, Table, ViewHeader } from '../components/ui'

// ---- shapes -----------------------------------------------------------------
// Declared here rather than in lib/types.ts: only this view reads them, and the
// per-view response shapes are deliberately kept next to their single consumer.

/**
 * One row of `/console`'s `sessions` list (`snapshot.py: build_console`, capped at
 * 50 and sorted by `lastMessageAt`). Note the aggregate ships session SUMMARIES
 * only — `list_sessions` strips `messages` — which is why opening a row costs a
 * second request.
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

/**
 * `sessions` arrives inside the `/console` aggregate but is not declared on
 * `ConsoleState` yet, and this view must not edit `lib/types.ts`. Narrowing here
 * keeps the read type-checked instead of reaching through `any`; the field belongs
 * on `ConsoleState` and is flagged for the shell owner.
 */
type StateWithSessions = ConsoleState & { sessions?: SessionSummary[] }

const CHANNEL_ICON: Record<string, LucideIcon> = {
  slack: Slack,
  email: Mail,
  webhook: Webhook,
  web: Globe,
  sms: MessageCircle,
}

/**
 * Conversations — the session list plus an on-demand transcript.
 *
 * The list is a pure function of the shell's poll (`state.sessions`), so it needs
 * no fetch of its own; only the transcript is loaded, and only when a row is
 * opened. Rows are keyed by session id, so a poll that reorders the list (it is
 * sorted by last activity, so it reorders often) cannot move the selection or
 * clobber the open thread the way the legacy `innerHTML` re-render did.
 */
export function ConversationsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const sessions = (state as StateWithSessions).sessions ?? []
  const [thread, setThread] = useState<SessionDetail | null>(null)
  const [loadingId, setLoadingId] = useState<string | null>(null)

  // Monotonic request token. Clicking a second conversation while the first is
  // still in flight must not let the slower response win: without this, the
  // transcript on screen can end up belonging to a row nobody selected.
  const seq = useRef(0)

  async function openThread(s: SessionSummary) {
    const mine = ++seq.current
    setLoadingId(s.id)
    try {
      // Workspace-scoped, NOT agent-prefixed: the session id already identifies
      // the agent, and the server only serves `/sessions/{id}` unprefixed.
      const detail = await api<SessionDetail>(`/sessions/${encodeURIComponent(s.id)}`)
      if (mine !== seq.current) return
      setThread(detail)
    } catch (e) {
      if (mine !== seq.current) return
      onToast(`Conversation error — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      if (mine === seq.current) setLoadingId(null)
    }
  }

  const messages = thread?.messages ?? []

  return (
    <>
      <ViewHeader title="Conversations">
        Durable, threaded sessions. Inbound events are grouped by identity, so an agent
        remembers a conversation across processes and restarts.
      </ViewHeader>

      <Table
        rows={sessions}
        rowKey={(s) => s.id}
        onRowClick={(s) => void openThread(s)}
        rowLabel={(s) => `Open conversation ${s.title || s.id}`}
        emptyIcon={MessagesSquare}
        // An empty list is an ordinary state on a fresh install, not a fault:
        // sessions appear the first time something sends the agent a message.
        emptyMessage="No conversations yet — inbound events create sessions automatically."
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

      {loadingId && !thread && <Empty>Reading transcript…</Empty>}

      {thread && (
        <>
          <SecRow
            left={
              <>
                {thread.title || 'Conversation'} · <span className="mono">{thread.channel || ''}</span>
              </>
            }
            right={
              <span className="mono">
                {messages.length} messages{thread.externalId ? ` · ${thread.externalId}` : ''}
              </span>
            }
          />
          <div className="window" style={{ padding: '14px 16px' }}>
            {messages.length === 0 ? (
              // An existing session with no messages is a real answer, and a
              // different one from "no such session" (which arrives as a 404 and
              // becomes a toast above).
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
                    // so this stays stable across a poll.
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
        </>
      )}
    </>
  )
}
