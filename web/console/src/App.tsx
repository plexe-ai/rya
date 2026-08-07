import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import './styles.css'

import {
  ApiError,
  api,
  clearAuth,
  getSession,
  getToken,
  getUserToken,
  isUnauthenticated,
  mintUserToken,
  onUnauthorized,
  resetRuntimeInfo,
  runtimeInfo,
} from './lib/api'
import { useNow, usePoll } from './lib/usePoll'
import { RefreshSignal } from './lib/refresh'
import { ALL_VIEWS } from './lib/nav'
import type { NavCounts, ViewId } from './lib/nav'
import { hasAgent } from './lib/types'
import { ag as agentPath, readAgent, writeAgent } from './lib/agent'
import type { ConsoleResponse, QueueCounts } from './lib/types'
import { AgentChooser } from './components/AgentChooser'

import { AuthModal } from './components/AuthModal'
import { ViewErrorBoundary } from './components/ErrorBoundary'
import { Sidebar } from './components/Sidebar'
import { StaleBanner, STALE_AFTER_FAILURES } from './components/StaleBanner'
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

/**
 * The session boundary — and the whole of the §5.11 fix.
 *
 * Signing out used to mean "remove four keys from localStorage". It removed the
 * CREDENTIALS and nothing the credentials had fetched: the previous tenant's runs,
 * secrets, traces, queue depths and every open detail panel stayed mounted behind
 * the dialog, one Escape away from being read by whoever signed in next — and
 * `rya_agent` survived to be sent as the *next* tenant's first request.
 *
 * Clearing those by hand would work until the next cache is added. So a session is
 * modelled as a component lifetime instead: `Console` is keyed on an epoch, and
 * signing out bumps it. React discards the entire subtree, which is every piece of
 * state in this file plus every piece of view-local state below it, including the
 * ones nobody has written yet. There is no list to keep in sync because there is no
 * list.
 *
 * `<App/>` still renders the console, so nothing else — tests included — has to know
 * this boundary is here.
 */
export default function App() {
  const [session, setSession] = useState(0)
  return <Console key={session} onSignedOut={() => setSession((s) => s + 1)} />
}

function Console({ onSignedOut }: { onSignedOut: () => void }) {
  const [view, setView] = useState<ViewId>(viewFromHash)
  const [navOpen, setNavOpen] = useState(false)
  /**
   * The refresh signal, broadcast to every loader BELOW this component
   * (`lib/refresh.ts`). See `refreshAll`, which is what everything calls.
   */
  const [refreshTick, setRefreshTick] = useState(0)

  /**
   * The auth gate, decided by the RUNTIME rather than by this browser (§5.12).
   *
   * `useState(!getToken())` was the bug in one expression: it answered "does this
   * runtime need a credential?" with "does this browser have one?". Those come apart
   * on the single most common way to run Rya — a plain `rya serve` with no
   * `RYA_TOKEN`, where `auth_enabled()` is false — and the console opened on a modal
   * demanding a token the server neither wants nor validates, with no visible way out.
   *
   * `/v1/info` has always answered it. Two states, and the second is the one that did
   * not exist before:
   *  - `gateChecked` false — we have not asked yet. The poll stays off and the shell
   *    paints its ordinary "Loading…", because a console that has not decided whether
   *    it may talk to the runtime must not talk to the runtime. One RTT, and only on a
   *    tokenless boot; a browser that already holds a token skips the probe entirely.
   *  - `authOpen` — the dialog, opened only when the runtime says it wants a
   *    credential, or when the probe could not be made at all (`runtimeInfo()`
   *    resolves to `{}` and an absent `authRequired` is read as "ask"). A discovery
   *    endpoint that cannot be reached is not evidence that a door is open.
   *
   * A 401 mid-session still opens the dialog through `onUnauthorized` below — that
   * path is the runtime's own answer and is unaffected by any of this.
   */
  const [gateChecked, setGateChecked] = useState(() => !!getToken())
  const [authOpen, setAuthOpen] = useState(false)
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

  // Why the dialog opened, in the server's words. A 401 can mean the token is stale,
  // but it can equally mean "JWT required." on a runtime where `RYA_JWT_SECRET` is
  // set and the operator token no longer authenticates anything — in which case the
  // dialog's own copy ("This runtime requires an operator token") is actively
  // misleading and the reason is the only thing that makes it navigable.
  const [authReason, setAuthReason] = useState<string | null>(null)
  useEffect(
    () =>
      onUnauthorized((e) => {
        setAuthReason(e.reason ? (e.hint ? `${e.reason} ${e.hint}` : e.reason) : null)
        setAuthOpen(true)
      }),
    [],
  )

  /** Opening the dialog deliberately: drop any reason left over from a past 401. */
  const openAuth = useCallback(() => {
    setAuthReason(null)
    setAuthOpen(true)
  }, [])

  // Ask the runtime once, on a tokenless boot. `runtimeInfo()` never rejects, so
  // `gateChecked` is set on every path — there is no way for this to leave the console
  // stuck on "Loading…" waiting for a decision that never comes.
  useEffect(() => {
    if (gateChecked) return
    let alive = true
    void runtimeInfo().then((info) => {
      if (!alive) return
      if (info.authRequired !== false) setAuthOpen(true)
      setGateChecked(true)
    })
    return () => {
      alive = false
    }
  }, [gateChecked])

  /**
   * May the console talk to the runtime yet?
   *
   * One name for the condition, used by all three loaders in this file, because they
   * were three separate spellings of `!authOpen` and a fourth loader added later would
   * have been a fourth. Before the gate has decided, the answer is no.
   */
  const canFetch = gateChecked && !authOpen

  // A session that outlived its user JWT — the common case for a browser returning the
  // next day, since the session lasts longer than the token's 12 hours. Minting here
  // rather than only in the auth modal is what makes attribution survive a reload:
  // without it the console would run unattributed until the operator signed in again,
  // which they have no reason to do because they are already signed in.
  useEffect(() => {
    if (getSession() && !getUserToken()) void mintUserToken()
  }, [])

  // A blip should warn once, not every 6 seconds; `wasLive` makes it edge-only.
  const wasLive = useRef<boolean | null>(null)
  const onPollError = useCallback(
    (e: Error) => {
      // Any 401 — not just a credential one. This used to compare `e.name`, which
      // silently stopped covering the whole condition once a 401 could also arrive as
      // an `ApiError`, and would have reported "Lost connection" for a server that
      // answered perfectly well.
      if (isUnauthenticated(e)) return
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

  const {
    data: state,
    error,
    loading,
    live,
    lastSuccessAt,
    failures,
    refresh,
  } = usePoll<ConsoleResponse>(load, POLL_MS, {
    enabled: canFetch,
    onError: onPollError,
    // The shell publishes the signal, so it cannot consume it from context — see
    // `opts.tick`. This is what makes ONE bump refresh the whole page, this poll
    // included, with exactly one request each.
    tick: refreshTick,
  })

  /**
   * "Everything on screen may no longer be true — read it all again."
   *
   * The only refresh in the console. Every loader in the tree honours it, so there
   * is no way left to wire up a refresh that reaches half the page — which is
   * precisely what the Refresh button had been doing (§5.6).
   */
  const refreshAll = useCallback(() => setRefreshTick((t) => t + 1), [])

  /**
   * The staleness clock (§5.9), running only while the runtime is silent and there
   * is something aged to describe. One clock for both readouts — the `live` pill and
   * the banner — so they cannot disagree about how old the data is.
   */
  const now = useNow(1000, !live && lastSuccessAt != null)
  const stale = !live && lastSuccessAt != null && failures >= STALE_AFTER_FAILURES

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

  // Queue depth comes from its own endpoint, so it rides alongside the main poll
  // rather than blocking it — and it is polled HERE, once, for both of its readers:
  // the sidebar badge and the Queue view's tiles. The Queue view used to fetch
  // `/queue/stats` itself, on entry and never again, so the tiles and the badge a
  // few pixels away showed different numbers, the tiles being the stale one
  // (audit §5.5). One poll cannot disagree with itself.
  //
  // `usePoll` rather than another hand-rolled interval because the distinction the
  // tiles need — not known yet / known and zero / known but the last refresh failed
  // — is exactly what it already models, and hand-rolling it is how the two readers
  // came to have different semantics in the first place.
  const loadQueue = useCallback(() => api<{ counts?: QueueCounts }>('/queue/stats'), [])
  const {
    data: queueData,
    error: queueError,
    loading: queueLoading,
  } = usePoll<{ counts?: QueueCounts }>(loadQueue, POLL_MS, { enabled: canFetch })

  // `null` until the endpoint has answered once, and never coerced to `{}`: an
  // unknown count and a count of zero are different answers, and "Dead-letter 0"
  // while the endpoint is failing is the most dangerous thing this console can say
  // (audit §5.4). The sidebar consumes it the same way — see `counts` below.
  const queue = queueData?.counts ?? null

  // Sidebar counts for the deploy group. Failures are still swallowed rather than
  // toasted — a count is decoration, and crying outage over a number nobody was
  // reading is its own bug — but they are no longer swallowed SILENTLY: a failed
  // read becomes `null`, which the sidebar draws as `—`.
  //
  // Three states, and the outer `null` is the third one. `{}` cannot express
  // "nothing has been attempted yet", so a first paint before the first tick would
  // otherwise be indistinguishable from three failed requests, and the sidebar would
  // flash `— — —` on every load of a perfectly healthy console (§5.10).
  type DeployCounts = { envs: number | null; versions: number | null; workers: number | null }
  const [deployCts, setDeployCts] = useState<DeployCounts | null>(null)
  useEffect(() => {
    if (!canFetch || !agent) return
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
        envs: envs?.environments?.length ?? null,
        versions: versions?.versions?.length ?? null,
        // `status=` empty on purpose: every status, not the `alive` default. An empty
        // list is scale-to-zero, and a crashed worker filtered out would make an
        // outage look identical to the designed idle state.
        workers: workers?.workers?.length ?? null,
      })
    }
    void tick()
    const id = setInterval(tick, DEPLOY_COUNT_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
    // `refreshTick` so Refresh re-reads these too. They are on a 30s timer of their
    // own, which is a long time to keep showing a number an operator has just asked
    // to have checked.
  }, [canFetch, agent, ag, refreshTick])

  const counts = useMemo((): NavCounts => {
    // Queue depth is workspace-scoped, so it is known even with no agent selected;
    // everything else is agent-scoped and simply absent until one is.
    //
    // Never a fabricated 0. Three cases, and the middle one is the §5.4/§5.10 point:
    // nothing settled yet (no badge — the console is still loading and saying so
    // elsewhere), settled without an answer (`null`, drawn as `—`), and a real
    // number. Amber follows the failed count, so it cannot fire on a guess.
    const queueCount: NavCounts = queueLoading
      ? {}
      : queue
        ? {
            queue: {
              value: (queue.pending ?? 0) + (queue.running ?? 0),
              amber: (queue.failed ?? 0) > 0,
            },
          }
        : { queue: { value: null } }
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
      // Absent until the first tick has settled; `null` — drawn as `—` — once it
      // has and the read failed.
      ...(deployCts === null
        ? {}
        : {
            envs: { value: deployCts.envs },
            versions: { value: deployCts.versions },
            workers: { value: deployCts.workers },
          }),
      ...queueCount,
    }
  }, [loaded, queue, queueLoading, deployCts])

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
      // Everything, not just the aggregate: the run this just created belongs in the
      // Runs table, which owns its own paged fetch since §5.1.
      refreshAll()
    } catch (e) {
      showToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  /**
   * End the session (§5.11).
   *
   * Order matters: storage first, then the remount. `Console` reads `getToken()` and
   * `readAgent()` during its initial render, so anything still in localStorage when
   * the new instance mounts is something the new instance adopts.
   *
   * `rya_agent` goes too. It is not a credential, which is why `clearAuth` never
   * touched it, but it is the *previous tenant's* agent name — and the next sign-in
   * would put it straight into `/console?agent=…`, asking a workspace that has never
   * heard of it for an agent it does not serve.
   */
  function signOut() {
    clearAuth()
    writeAgent(null)
    // A new session may be pointed at a different runtime; the cached `/v1/info` is
    // an answer from the old one.
    resetRuntimeInfo()
    onSignedOut()
  }

  return (
    // Every loader below this line honours Refresh, without knowing it exists.
    <RefreshSignal.Provider value={refreshTick}>
      <div className="app">
        <Sidebar
          view={view}
          onNavigate={navigate}
          state={state}
          roster={roster}
          // Two names, deliberately, and §5.14 is what happens when they are one.
          // `selected` is the operator's CHOICE — what they clicked, what is in
          // localStorage, and what every agent-scoped request in this console is
          // addressed to. `showing` is the server's ECHO — whose data is on the page.
          //
          // The `<select>` was bound to the echo, so choosing an agent snapped the
          // control back to the old name for a full round trip, and if that request
          // failed it stayed there: the selector said A, `ag()` was addressing B, and
          // nothing on screen admitted the difference. Binding it to the choice fixes
          // the snap-back but opens a second way to lie — the control now names an
          // agent the page is not showing — so the Sidebar is given both, and says so
          // whenever they disagree.
          selected={agent}
          showing={loaded?.agent.name ?? null}
          switchFailed={!live}
          onSelectAgent={selectAgent}
          counts={counts}
          open={navOpen}
          onWorkspaceClick={openAuth}
          onSignOut={signOut}
        />

        <main className="main">
          <TopBar
            state={state}
            live={live}
            loading={loading}
            lastSuccessAt={lastSuccessAt}
            now={now}
            onRefresh={refreshAll}
            onSendEvent={() => void sendEvent()}
            onToggleNav={() => setNavOpen((o) => !o)}
          />

          <div className="scroll">
            {/* Keyed on the agent the DATA belongs to, which remounts the view — and
                with it every panel of view-local detail state — the moment the
                console starts showing a different agent.

                This is the whole of the §5.3 fix, and it is one line because the bug
                was structural: views were unkeyed, so React preserved their state
                across an agent switch and the trace, thread, turn, environment,
                version and eval-result panels all kept the previous agent's data
                under the new agent's header. In Guard it was worse than confusing —
                Save wrote agent A's draft into agent B's policy.

                Keyed on `loaded.agent.name` rather than on the `agent` SELECTION on
                purpose. The selection changes immediately on click while the fetch
                is still in flight; keying on it would blank the panels a beat before
                the new data arrives, and would remount once more when the shell
                adopts a sole agent's name on first load. The server echo changes
                exactly when the content does. An unsaved Guard draft is discarded on
                a switch — deliberate, and strictly better than writing it to the
                wrong agent. */}
            {/* Above the keyed subtree and OUTSIDE the error boundary: staleness is
                a property of the shell's poll, not of whichever view is open, and it
                is exactly as true of a view that has crashed. */}
            {stale && lastSuccessAt != null && (
              <div className="wrap" style={{ paddingBottom: 0 }}>
                <StaleBanner
                  lastSuccessAt={lastSuccessAt}
                  now={now}
                  message={error}
                  onRetry={refreshAll}
                />
              </div>
            )}
            <div className="view wrap" key={loaded?.agent.name ?? '_none'}>
              {/* Inside the shell, around the view and nothing else. A render throw
                  in one view must not take the sidebar with it: React 19 unmounts
                  the whole tree otherwise, and since the hash is the route, a reload
                  lands right back on the view that threw. Resets on navigation and
                  on an agent switch — see ViewErrorBoundary. */}
              <ViewErrorBoundary view={view} agent={agent} onHome={() => navigate('overview')}>
                {/* Only paint the down-state when there is no data at all — never
                    clobber a view an operator is reading with a stale-but-useful
                    table (console/AGENTS.md). */}
                {!state && !loading && error ? (
                  <RuntimeDown message={error} onRetry={refreshAll} onEnterToken={openAuth} />
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
                  <ApprovalsView
                    state={loaded}
                    agent={loaded.agent.name}
                    onToast={showToast}
                    onResolved={refresh}
                  />
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
                  /* The tiles read the shell's poll rather than fetching the same
                     endpoint again on their own — see `loadQueue` above. */
                  <QueueView
                    state={loaded}
                    onToast={showToast}
                    stats={{ counts: queue, error: queueError, loading: queueLoading }}
                  />
                ) : view === 'governance' ? (
                  <GovernanceView state={loaded} onToast={showToast} />
                ) : view === 'guard' ? (
                  <GuardView state={loaded} onToast={showToast} />
                ) : view === 'team' ? (
                  <TeamView state={loaded} onToast={showToast} onSignIn={openAuth} />
                ) : (
                  assertAllViewsHandled(view)
                )}
              </ViewErrorBoundary>
            </div>
          </div>
        </main>
      </div>

      <Toast message={toast} />

      {authOpen && (
        <AuthModal
          reason={authReason}
          onClose={() => setAuthOpen(false)}
          onAuthed={() => {
            setAuthOpen(false)
            // A new credential can change what every endpoint answers, not just
            // `/console` — a view that 401'd on entry has to be given its second try.
            refreshAll()
          }}
        />
      )}
    </RefreshSignal.Provider>
  )
}
