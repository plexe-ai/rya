import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import './styles.css'

import { ApiError, api, clearAuth, getToken, onUnauthorized } from './lib/api'
import { usePoll } from './lib/usePoll'
import { ALL_VIEWS } from './lib/nav'
import type { CountKey, ViewId } from './lib/nav'
import { hasAgent } from './lib/types'
import { ag as agentPath, readAgent, writeAgent } from './lib/agent'
import type { ConsoleResponse, QueueCounts } from './lib/types'
import { AgentChooser } from './components/AgentChooser'

import { AuthModal } from './components/AuthModal'
import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { Empty, Toast } from './components/ui'

import { OverviewView, RuntimeDown } from './views/Overview'
import { RunsView } from './views/Runs'
import { ApprovalsView } from './views/Approvals'
import { ChannelsView, JobsView, ManifestView, MemoryView, ModelsView, SecretsView } from './views/simple'
import { ConnectionsView } from './views/Connections'
import { ConversationsView } from './views/Conversations'
import { EnvironmentsView, VersionsView, WorkersView } from './views/Deploy'
import { EvalsView } from './views/Evals'
import { GovernanceView } from './views/Governance'
import { GuardView } from './views/Guard'
import { InfrastructureView } from './views/Infrastructure'
import { KnowledgeView } from './views/Knowledge'
import { QuotasView } from './views/Quotas'
import { QueueView } from './views/Queue'
import { TeamView } from './views/Team'
import { ToolsView } from './views/Tools'

const POLL_MS = 6000

// Deployment topology moves on a promote, not per second, so the envs/versions/workers
// counts get their own slow timer instead of riding the 6s poll — the same 30s the
// legacy console used (`refreshDeployCts`) and for the same reason. Three extra reads
// every six seconds would be three reads too many for a number in a sidebar.
const DEPLOY_COUNT_MS = 30_000

// The selected agent is remembered per browser under `rya_agent`. Reading, writing and
// path-prefixing live in lib/agent.ts so a VIEW can make an agent-scoped call without
// importing the shell — see `ag()` there for why the prefix is not optional.

/**
 * The end of the view chain, typed `never`.
 *
 * This is what replaced `NotYetMigrated`. While the port was in flight the fallback
 * arm rendered "not migrated yet" for any view without a component, which meant
 * forgetting to wire one up looked like a deliberate state. Now every `ViewId` has a
 * branch, so `view` is narrowed to `never` here — and adding a nav entry without a
 * branch is a BUILD error instead of a blank page.
 *
 * It still renders something, because the hash is user-editable: `viewFromHash`
 * validates against `ALL_VIEWS`, so this is unreachable in practice, and throwing to
 * prove a point would turn a typo in the address bar into a white screen.
 */
function assertAllViewsHandled(view: never): ReactElement {
  return <Empty>Unknown view “{String(view)}”.</Empty>
}

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

  const [agent, setAgentState] = useState<string | null>(readAgent)

  const selectAgent = useCallback((name: string | null) => {
    writeAgent(name)
    setAgentState(name)
  }, [])

  /** `/agents/{selected}{path}` — every agent-scoped call must go through this. */
  const ag = useCallback((path: string) => agentPath(agent ?? '_', path), [agent])

  const load = useCallback(async (): Promise<ConsoleResponse> => {
    const qs = agent ? `?agent=${encodeURIComponent(agent)}` : ''
    try {
      return await api<ConsoleResponse>(`/console${qs}`)
    } catch (e) {
      // A remembered selection the workspace does not serve is NOT an outage — it is
      // what happens when the same browser is pointed at a different tenant. Drop it
      // and retry once, so the page lands on the picker rather than on a
      // "can't reach the runtime" card that sends you to inspect Docker.
      if (e instanceof ApiError && e.code === 'E_AGENT_NOT_FOUND' && agent) {
        writeAgent(null)
        setAgentState(null)
        showToast(`"${agent}" is not served here — pick an agent`)
        return await api<ConsoleResponse>('/console')
      }
      throw e
    }
  }, [agent, showToast])

  const { data: state, error, loading, live, refresh } = usePoll<ConsoleResponse>(
    load,
    POLL_MS,
    { enabled: !authOpen, onError: onPollError },
  )

  // The narrowing every view depends on. `state` may legitimately carry no agent;
  // `loaded` is the shape that has one.
  const loaded = hasAgent(state) ? state : null
  const roster = state?.agents ?? []

  // A workspace that serves exactly one agent needs no choosing — the server already
  // selected it, so adopt that name rather than leaving the UI in an "unset" state
  // that would send `_` on the next agent-scoped call.
  useEffect(() => {
    if (!agent && loaded?.agent?.name) selectAgent(loaded.agent.name)
  }, [agent, loaded, selectAgent])

  // Switching agents has to refetch NOW. `usePoll` keeps its fetcher in a ref on
  // purpose — changing it must not restart the interval — so without this the new
  // selection would sit unapplied for up to a full poll period, and the page would
  // keep showing the previous agent's runs under the new agent's name.
  const firstPoll = useRef(true)
  useEffect(() => {
    if (firstPoll.current) {
      firstPoll.current = false
      return
    }
    void refresh()
  }, [agent, refresh])
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

  // Sidebar counts for the deploy group. Failures are swallowed on purpose: a count is
  // decoration, and a toast for one would cry outage over a number nobody was reading.
  const [deployCts, setDeployCts] = useState<{ envs?: number; versions?: number; workers?: number }>({})
  useEffect(() => {
    if (authOpen || !agent) return
    let alive = true
    const one = <T,>(path: string) => api<T>(path).catch(() => null)
    const tick = async () => {
      const [envs, versions, workers] = await Promise.all([
        one<{ environments?: unknown[] }>(ag('/environments')),
        one<{ versions?: unknown[] }>(ag('/versions')),
        one<{ workers?: unknown[] }>('/workers?status='),
      ])
      if (!alive) return
      setDeployCts({
        envs: envs?.environments?.length,
        versions: versions?.versions?.length,
        // `status=` empty on purpose: every status, not the `alive` default. An empty
        // list is scale-to-zero, and a crashed worker filtered out would make an
        // outage look identical to the designed idle state.
        workers: workers?.workers?.length,
      })
    }
    void tick()
    const id = setInterval(tick, DEPLOY_COUNT_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [authOpen, agent, ag])

  const counts = useMemo((): Partial<Record<CountKey, { value: number; amber?: boolean }>> => {
    // Queue depth is workspace-scoped, so it is known even with no agent selected;
    // everything else is agent-scoped and simply absent until one is.
    const queueCount = {
      queue: {
        value: (queue.pending ?? 0) + (queue.running ?? 0),
        amber: (queue.failed ?? 0) > 0,
      },
    }
    if (!loaded) return queueCount
    const violations = loaded.governance?.violations?.length ?? 0
    return {
      tools: { value: loaded.tools.length },
      models: { value: loaded.models.length },
      channels: { value: loaded.channels.length },
      memory: { value: loaded.memory.collections.length },
      knowledge: { value: loaded.knowledge?.documents.length ?? 0 },
      connections: { value: (loaded.connections ?? []).length },
      secrets: { value: loaded.secrets.length },
      sessions: { value: loaded.stats.sessions ?? 0 },
      approvals: { value: loaded.stats.approvalsPending, amber: loaded.stats.approvalsPending > 0 },
      violations: { value: violations, amber: violations > 0 },
      ...(deployCts.envs === undefined ? {} : { envs: { value: deployCts.envs } }),
      ...(deployCts.versions === undefined ? {} : { versions: { value: deployCts.versions } }),
      ...(deployCts.workers === undefined ? {} : { workers: { value: deployCts.workers } }),
      ...queueCount,
    }
  }, [loaded, queue, deployCts])

  useEffect(() => {
    const name = state?.branding?.name ?? 'Rya'
    document.title = loaded
      ? `${name} · ${loaded.agent.name}`
      : state
        ? `${name} · no agent selected`
        : 'Rya — Agent backend console'
  }, [state, loaded])

  async function sendEvent() {
    try {
      // Agent-PREFIXED. The unprefixed spelling still answers via the deprecated
      // Rule 6 fallback, which resolves the `_` alias — so it 400s with
      // E_AGENT_AMBIGUOUS the moment a workspace serves a second agent.
      await api(ag('/events'), {
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
          roster={roster}
          selected={loaded?.agent.name ?? null}
          onSelectAgent={selectAgent}
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
              ) : !loaded ? (
                /* A real state, not an error: nothing published yet, or several
                   agents and none chosen. Every view below needs a selected agent,
                   so this stands in for all of them rather than each guarding. */
                <AgentChooser roster={roster} onSelect={selectAgent} />
              ) : view === 'overview' ? (
                <OverviewView state={loaded} onNavigate={navigate} />
              ) : view === 'runs' ? (
                <RunsView state={loaded} onToast={showToast} />
              ) : view === 'approvals' ? (
                <ApprovalsView state={loaded} onToast={showToast} onResolved={refresh} />
              ) : view === 'manifest' ? (
                <ManifestView state={loaded} />
              ) : view === 'models' ? (
                <ModelsView state={loaded} />
              ) : view === 'channels' ? (
                <ChannelsView state={loaded} />
              ) : view === 'secrets' ? (
                <SecretsView state={loaded} />
              ) : view === 'memory' ? (
                <MemoryView state={loaded} />
              ) : view === 'jobs' ? (
                <JobsView state={loaded} />
              ) : view === 'infra' ? (
                <InfrastructureView state={loaded} onToast={showToast} />
              ) : view === 'tools' ? (
                <ToolsView state={loaded} onToast={showToast} />
              ) : view === 'knowledge' ? (
                <KnowledgeView state={loaded} onToast={showToast} />
              ) : view === 'connections' ? (
                <ConnectionsView state={loaded} onToast={showToast} />
              ) : view === 'deploy' ? (
                <EnvironmentsView state={loaded} onToast={showToast} />
              ) : view === 'versions' ? (
                <VersionsView state={loaded} onToast={showToast} />
              ) : view === 'workers' ? (
                <WorkersView state={loaded} onToast={showToast} />
              ) : view === 'quotas' ? (
                <QuotasView state={loaded} onToast={showToast} />
              ) : view === 'conversations' ? (
                <ConversationsView state={loaded} onToast={showToast} />
              ) : view === 'evals' ? (
                <EvalsView state={loaded} onToast={showToast} />
              ) : view === 'queue' ? (
                <QueueView state={loaded} onToast={showToast} />
              ) : view === 'governance' ? (
                <GovernanceView state={loaded} onToast={showToast} />
              ) : view === 'guard' ? (
                <GuardView state={loaded} onToast={showToast} />
              ) : view === 'team' ? (
                <TeamView state={loaded} onToast={showToast} onSignIn={() => setAuthOpen(true)} />
              ) : (
                assertAllViewsHandled(view)
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
