import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './styles.css'

import { api, clearAuth, getToken, onUnauthorized } from './lib/api'
import { usePoll } from './lib/usePoll'
import { NAV, ALL_VIEWS } from './lib/nav'
import type { CountKey, ViewId } from './lib/nav'
import type { ConsoleState, QueueCounts } from './lib/types'

import { AuthModal } from './components/AuthModal'
import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { Toast } from './components/ui'

import { OverviewView, RuntimeDown } from './views/Overview'
import { RunsView } from './views/Runs'
import { ApprovalsView } from './views/Approvals'
import {
  ChannelsView, JobsView, ManifestView, MemoryView, ModelsView, NotYetMigrated, SecretsView,
} from './views/simple'

const POLL_MS = 6000

const LABEL = new Map<ViewId, string>(NAV.flatMap((g) => g.items.map((i) => [i.id, i.label] as const)))

function viewFromHash(): ViewId {
  const h = location.hash.replace(/^#/, '')
  return (ALL_VIEWS as string[]).includes(h) ? (h as ViewId) : 'overview'
}

export default function App() {
  const [view, setView] = useState<ViewId>(viewFromHash)
  const [navOpen, setNavOpen] = useState(false)
  const [authOpen, setAuthOpen] = useState(!getToken())
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  const showToast = useCallback((m: string) => {
    setToast(m)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 2600)
  }, [])

  // Deep-linkable views: the hash IS the route, so a reload or a shared link
  // lands on the same page. The legacy console had no route at all.
  useEffect(() => {
    const onHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navigate = useCallback((v: ViewId) => {
    location.hash = v
    setView(v)
    setNavOpen(false)
    // `scrollTop =` rather than `scrollTo()`: same behaviour as the legacy console
    // and universally available, where `Element.scrollTo` is not.
    const scroller = document.querySelector('.scroll')
    if (scroller) scroller.scrollTop = 0
  }, [])

  useEffect(() => onUnauthorized(() => setAuthOpen(true)), [])

  // A blip should warn once, not every 6 seconds; `wasLive` makes it edge-only.
  const wasLive = useRef<boolean | null>(null)
  const onPollError = useCallback(
    (e: Error) => {
      if (e.name === 'UnauthorizedError') return
      if (wasLive.current !== false) showToast('Lost connection to the runtime — retrying…')
      wasLive.current = false
    },
    [showToast],
  )

  const { data: state, error, loading, live, refresh } = usePoll<ConsoleState>(
    () => api<ConsoleState>('/console'),
    POLL_MS,
    { enabled: !authOpen, onError: onPollError },
  )
  useEffect(() => {
    if (live) wasLive.current = true
  }, [live])

  // Sidebar queue count comes from its own endpoint, so it rides along with the
  // main poll rather than blocking it.
  const [queue, setQueue] = useState<QueueCounts>({})
  useEffect(() => {
    if (authOpen) return
    let alive = true
    const tick = () =>
      api<{ counts?: QueueCounts }>('/queue/stats')
        .then((d) => alive && setQueue(d.counts ?? {}))
        .catch(() => {})
    void tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [authOpen])

  const counts = useMemo((): Partial<Record<CountKey, { value: number; amber?: boolean }>> => {
    if (!state) return {}
    const violations = state.governance?.violations?.length ?? 0
    return {
      tools: { value: state.tools.length },
      models: { value: state.models.length },
      channels: { value: state.channels.length },
      memory: { value: state.memory.collections.length },
      knowledge: { value: state.knowledge?.documents.length ?? 0 },
      connections: { value: (state.connections ?? []).length },
      secrets: { value: state.secrets.length },
      sessions: { value: state.stats.sessions ?? 0 },
      approvals: { value: state.stats.approvalsPending, amber: state.stats.approvalsPending > 0 },
      violations: { value: violations, amber: violations > 0 },
      queue: {
        value: (queue.pending ?? 0) + (queue.running ?? 0),
        amber: (queue.failed ?? 0) > 0,
      },
    }
  }, [state, queue])

  useEffect(() => {
    const name = state?.branding?.name ?? 'Rya'
    document.title = state ? `${name} · ${state.agent.name}` : 'Rya — Agent backend console'
  }, [state])

  async function sendEvent() {
    try {
      await api('/events', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ type: 'message.received', payload: { email: 'ada@example.com' } }),
      })
      showToast('Event sent')
      await refresh()
    } catch (e) {
      showToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  function signOut() {
    clearAuth()
    setAuthOpen(true)
  }

  return (
    <>
      <div className="app">
        <Sidebar
          view={view}
          onNavigate={navigate}
          state={state}
          counts={counts}
          open={navOpen}
          onWorkspaceClick={() => setAuthOpen(true)}
          onSignOut={signOut}
        />

        <main className="main">
          <TopBar
            state={state}
            live={live}
            loading={loading}
            onRefresh={() => void refresh()}
            onSendEvent={() => void sendEvent()}
            onToggleNav={() => setNavOpen((o) => !o)}
          />

          <div className="scroll">
            <div className="view wrap">
              {/* Only paint the down-state when there is no data at all — never
                  clobber a view an operator is reading with a stale-but-useful
                  table (console/AGENTS.md). */}
              {!state && !loading && error ? (
                <RuntimeDown
                  message={error}
                  onRetry={() => void refresh()}
                  onEnterToken={() => setAuthOpen(true)}
                />
              ) : !state ? (
                <div className="empty">Loading…</div>
              ) : view === 'overview' ? (
                <OverviewView state={state} onNavigate={navigate} />
              ) : view === 'runs' ? (
                <RunsView state={state} onToast={showToast} />
              ) : view === 'approvals' ? (
                <ApprovalsView state={state} onToast={showToast} onResolved={refresh} />
              ) : view === 'manifest' ? (
                <ManifestView state={state} />
              ) : view === 'models' ? (
                <ModelsView state={state} />
              ) : view === 'channels' ? (
                <ChannelsView state={state} />
              ) : view === 'secrets' ? (
                <SecretsView state={state} />
              ) : view === 'memory' ? (
                <MemoryView state={state} />
              ) : view === 'jobs' ? (
                <JobsView state={state} />
              ) : (
                <NotYetMigrated title={LABEL.get(view) ?? view} legacyHash={view} />
              )}
            </div>
          </div>
        </main>
      </div>

      <Toast message={toast} />

      {authOpen && (
        <AuthModal
          onClose={() => setAuthOpen(false)}
          onAuthed={() => {
            setAuthOpen(false)
            void refresh()
          }}
        />
      )}
    </>
  )
}
