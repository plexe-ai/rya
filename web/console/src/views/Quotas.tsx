import type { ReactNode } from 'react'
import { AlertTriangle, Box, Coins, Gauge, KeyRound, Package, Play, ShieldAlert, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api'
import { useLoad } from '../lib/usePoll'
import { num, usd } from '../lib/format'
import type { ConsoleState } from '../lib/types'
import { Empty, Mono, SecRow, Table, Tile, ViewHeader } from '../components/ui'

// ---- response shapes ---------------------------------------------------------
//
// Declared here rather than in lib/types.ts: these three endpoints have exactly one
// consumer, this page. Everything is optional because the server legitimately omits
// it — an unset ceiling means UNLIMITED, not zero.

/** `GET /quotas` -> `quota`: the resolved policy (`QuotaPolicy.describe`). */
interface QuotaPolicy {
  enforced?: boolean
  /** `policy` when the workspace has its own row, otherwise the platform default. */
  source?: string
  maxConcurrentRuns?: number | null
  maxRunsPerDay?: number | null
  maxQueueDepth?: number | null
  maxTokensPerDay?: number | null
  maxCostUsdPerDay?: number | null
  maxWorkers?: number | null
}

/** `GET /quotas` -> `usage`: what is being consumed right now, per ceiling. */
interface QuotaConsumption {
  concurrentRuns?: number | null
  runsToday?: number | null
  queueDepth?: number | null
  tokensToday?: number | null
  costUsdToday?: number | null
  workers?: number | null
}

/** One breached ceiling. This page only counts them; the table says which. */
interface QuotaViolation {
  limit?: string
  label?: string
  current?: number
  max?: number
  scope?: string
}

/** The org rollup (D29) — a derived row a reconciler writes, not a live query. */
interface OrgVerdict {
  orgId?: string
  exhausted?: boolean
  budget?: {
    maxTokensPerDay?: number | null
    maxCostUsdPerDay?: number | null
    maxCostUsdPerMonth?: number | null
  }
  usage?: {
    tokensToday?: number | null
    costUsdToday?: number | null
    costUsdMonth?: number | null
  }
  workspaces?: string[]
  computedAt?: string
  violations?: QuotaViolation[]
}

interface QuotasResponse {
  quota?: QuotaPolicy
  usage?: QuotaConsumption
  admission?: QuotaViolation[]
  /**
   * Present only once a reconciler has written a verdict. Absent is a state of its
   * own, not missing data — see `OrgBudget` below.
   */
  org?: OrgVerdict
}

/** `GET /usage` -> the durable meter's totals. Billable facts, not trace sums. */
interface MeterTotals {
  calls?: number
  inputTokens?: number
  outputTokens?: number
  costUsd?: number
}

/** One launch-gate condition: is it in force, and what did the check see? */
interface PostureCondition {
  ok?: boolean
  detail?: string
}

/** `GET /posture` — configuration only. Credential *kinds*, never values. */
interface PostureResponse {
  untrusted?: boolean
  ok?: boolean
  unmet?: string[]
  isolation?: PostureCondition
  broker?: PostureCondition
  egress?: PostureCondition
  probe?: { verified?: boolean | null; detail?: string } | null
  driver?: { driver?: string; isolation?: string }
  credentials?: { clean?: boolean; violations?: { group?: string; name?: string }[] }
}

/**
 * A verdict badge. `StatusBadge` derives its tone from a *run* status; the cells here
 * carry limit and gate verdicts with their own wording, so the tone is passed in.
 */
function Verdict({ tone, children }: { tone: 'ok' | 'wait' | 'fail'; children: ReactNode }) {
  return (
    <span className={`stbadge ${tone}`}>
      <span className="d" />
      {children}
    </span>
  )
}

/** Accessors rather than string keys, so an unset ceiling stays typed as unset. */
const LIMITS: {
  key: string
  label: string
  consuming: (u: QuotaConsumption) => number | null | undefined
  ceiling: (q: QuotaPolicy) => number | null | undefined
}[] = [
  { key: 'concurrentRuns', label: 'Concurrent runs', consuming: (u) => u.concurrentRuns, ceiling: (q) => q.maxConcurrentRuns },
  { key: 'runsToday', label: 'Runs today', consuming: (u) => u.runsToday, ceiling: (q) => q.maxRunsPerDay },
  { key: 'queueDepth', label: 'Pending queue jobs', consuming: (u) => u.queueDepth, ceiling: (q) => q.maxQueueDepth },
  { key: 'tokensToday', label: 'Tokens today', consuming: (u) => u.tokensToday, ceiling: (q) => q.maxTokensPerDay },
  { key: 'costUsdToday', label: 'USD today', consuming: (u) => u.costUsdToday, ceiling: (q) => q.maxCostUsdPerDay },
  { key: 'workers', label: 'Live workers', consuming: (u) => u.workers, ceiling: (q) => q.maxWorkers },
]

const ORG_LIMITS: {
  key: string
  label: string
  consuming: (u: NonNullable<OrgVerdict['usage']>) => number | null | undefined
  ceiling: (b: NonNullable<OrgVerdict['budget']>) => number | null | undefined
}[] = [
  { key: 'tokensToday', label: 'Org tokens today', consuming: (u) => u.tokensToday, ceiling: (b) => b.maxTokensPerDay },
  { key: 'costUsdToday', label: 'Org USD today', consuming: (u) => u.costUsdToday, ceiling: (b) => b.maxCostUsdPerDay },
  { key: 'costUsdMonth', label: 'Org USD this month', consuming: (u) => u.costUsdMonth, ceiling: (b) => b.maxCostUsdPerMonth },
]

/**
 * D29: the billing boundary above this workspace. Absent unless a reconciler has run,
 * which is deliberate — "no rollup" and "an all-clear rollup" are different states and
 * an operator refusing to believe a budget needs to tell them apart. Rendering an
 * all-clear here for a missing block would erase that distinction, so this returns
 * NOTHING when there is no `org`.
 */
function OrgBudget({ org }: { org?: OrgVerdict }) {
  if (!org || !org.orgId) return null
  const budget = org.budget ?? {}
  const used = org.usage ?? {}
  // Only the ceilings the org actually set: an unset one is unlimited, not a breach.
  const rows = ORG_LIMITS.filter((l) => l.ceiling(budget) != null)

  return (
    <>
      <SecRow
        left="Organization budget"
        right={`shared with ${(org.workspaces ?? []).length} workspace(s) — computed ${org.computedAt ?? 'never'}`}
      />
      <Table
        rows={rows}
        rowKey={(l) => l.key}
        emptyIcon={Coins}
        emptyMessage="This organization has no budget set."
        columns={[
          { header: 'Limit', cell: (l) => l.label },
          {
            header: 'Org consuming',
            cell: (l) => {
              const cur = l.consuming(used)
              return <Mono>{cur == null ? '—' : String(cur)}</Mono>
            },
          },
          { header: 'Ceiling', cell: (l) => <Mono>{String(l.ceiling(budget))}</Mono> },
          {
            header: 'Status',
            cell: (l) => {
              const cur = l.consuming(used)
              const lim = l.ceiling(budget)
              const over = cur != null && Number(cur) >= Number(lim)
              return over ? <Verdict tone="fail">over budget</Verdict> : <Verdict tone="ok">within budget</Verdict>
            },
          },
        ]}
      />
      {/* An operator told "quota exhausted" while this workspace's own usage sits near
          zero will look in the wrong place, so when the ORG is the boundary that
          refused, the page says which one in words. */}
      {org.exhausted && (
        <div className="keynote">
          This workspace is refusing new work because its <b>organization</b> is over budget, not
          because of its own quota. <span className="mono">rya orgs show</span> names which
          workspace spent it.
        </div>
      )}
    </>
  )
}

/** The three launch-gate conditions, in the order the gate states them. */
const CONDITIONS: { key: string; label: string; of: (p: PostureResponse) => PostureCondition | undefined }[] = [
  { key: 'isolation', label: 'Isolation (D23)', of: (p) => p.isolation },
  { key: 'broker', label: 'Credential mediation (D18)', of: (p) => p.broker },
  { key: 'egress', label: 'Network egress (D24)', of: (p) => p.egress },
]

/**
 * The launch gate, on the page that already answers "what is this deployment allowed
 * to do". Read-only and unauthenticated server-side, because an operator checking
 * whether their own deployment is safe should not need a token to find out — and every
 * field is about CONFIGURATION, never a credential's value.
 */
function TenantPosture({ posture }: { posture: PostureResponse }) {
  const p = posture
  const driver = p.driver ?? {}
  const creds = p.credentials ?? {}
  const verified = p.probe?.verified
  // A trusted deployment with none of the three conditions met is CORRECT. Marking it
  // red would train an operator to ignore the mark on the one deployment where it means
  // something, so the badge follows `untrusted` and not `ok`.
  const satisfied = p.ok || !p.untrusted

  return (
    <>
      <SecRow
        left="Tenant posture"
        right={p.untrusted ? 'untrusted tenancy declared' : 'trusted tenancy'}
      />
      <div className="stats" style={{ marginBottom: 20 }}>
        <Tile
          icon={satisfied ? ShieldCheck : ShieldAlert}
          label="Posture"
          value={p.untrusted ? (p.ok ? 'safe' : 'INCOMPLETE') : 'trusted'}
          sub={p.untrusted ? 'RYA_UNTRUSTED_TENANTS=1' : 'hostile-tenant isolation not claimed'}
          amber={!!p.untrusted && !p.ok}
        />
        <Tile
          icon={Box}
          label="Driver"
          value={driver.driver || '—'}
          // Declared isolation and PROBED isolation are different claims: a refuted
          // probe means the declaration is wrong, which is louder than not knowing.
          sub={`${driver.isolation || '—'}${
            verified === true ? ' · verified' : verified === false ? ' · REFUTED' : ' · unverified'
          }`}
          amber={verified === false}
        />
        <Tile
          icon={KeyRound}
          label="This process"
          value={creds.clean ? 'no platform credentials' : 'holds credentials'}
          // Credential KINDS, never values — the inventory is designed so this
          // response is safe to render, which is why the route needs no token.
          sub={
            creds.clean
              ? 'nothing a tenant must not see'
              : (creds.violations ?? []).map((v) => v.group || v.name).join(', ')
          }
        />
      </div>
      <Table
        rows={CONDITIONS}
        rowKey={(c) => c.key}
        columns={[
          { header: 'Condition', cell: (c) => c.label },
          { header: 'State', cell: (c) => c.of(p)?.detail || '—' },
          {
            header: 'Status',
            cell: (c) =>
              c.of(p)?.ok ? <Verdict tone="ok">in force</Verdict> : <Verdict tone="wait">not in force</Verdict>,
          },
        ]}
      />
      {!p.untrusted && (
        // `.note` in the legacy markup has no rule in styles.css; `.keynote` is the
        // closest existing class and this is not a restyle.
        <div className="keynote">
          None of the above is enforced: the trusted posture is supported and is what every
          self-host runs. Declare <span className="mono">RYA_UNTRUSTED_TENANTS=1</span> only with
          all three in force — the platform refuses to start otherwise.
        </div>
      )}
    </>
  )
}

/**
 * Quota &amp; usage.
 *
 * The page answers three questions, not one, and the order is deliberate: **this
 * workspace's** limits, then **its organization's** budget (D29), then the **launch
 * gate** (D18/D23/D24). Each comes from a different boundary, and an operator debugging
 * a refusal has to know which one refused.
 *
 * Loads on entry rather than from the shell's 6s poll: ceilings and a deployment's
 * posture move on a promote or a policy write, not per second.
 *
 * All three endpoints are workspace/deployment-scoped, so all three are plain `api()`
 * calls with no agent prefix — `/posture` in particular is a property of the deployment
 * rather than of an agent.
 */
export function QuotasView({
  state: _state,
  onToast: _onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const { data, error, loading } = useLoad(async () => {
    const quotas = await api<QuotasResponse>('/quotas')
    // These two are additions to the page, not the page itself. A failure of either
    // degrades to its own empty state instead of taking the ceilings down with it:
    // the meter may have no rows yet, and `/posture` is a separate route entirely.
    const meter = await api<{ usage?: MeterTotals }>('/usage')
      .then((r) => r.usage ?? null)
      .catch(() => null)
    const posture = await api<PostureResponse>('/posture').catch(() => null)
    return { quotas, meter, posture }
  })

  const header = (
    <ViewHeader title="Quota &amp; usage">
      What this workspace is allowed to consume, and what it is consuming. A tenant cannot raise
      its own limits — that is the point of a quota.
    </ViewHeader>
  )

  if (error || (!data && !loading)) {
    // Same split the legacy `deployErr` made: no token is a thing to fix, not an outage.
    const message = error ?? 'unavailable'
    return (
      <>
        {header}
        <Empty icon={Package}>
          {message === 'unauthorized'
            ? 'Connect with a workspace key to load Quota.'
            : `Quota unavailable — ${message}`}
        </Empty>
      </>
    )
  }
  if (!data) {
    return (
      <>
        {header}
        <Empty icon={Gauge}>Loading quota…</Empty>
      </>
    )
  }

  const { quotas, meter, posture } = data
  const policy = quotas.quota ?? {}
  const used = quotas.usage ?? {}
  const admission = quotas.admission ?? []

  return (
    <>
      {header}

      <div className="stats" style={{ marginBottom: 20 }}>
        <Tile
          icon={Gauge}
          label="Quota"
          value={policy.enforced ? 'enforced' : 'unlimited'}
          sub={policy.source === 'policy' ? 'workspace policy' : 'platform default'}
        />
        <Tile
          icon={Play}
          label="Runs today"
          value={used.runsToday != null ? used.runsToday : '—'}
          sub={`${used.concurrentRuns || 0} running now`}
        />
        <Tile
          icon={Coins}
          label="Tokens today"
          value={num(used.tokensToday)}
          sub={used.costUsdToday != null ? `${usd(used.costUsdToday)} today` : 'metered'}
        />
        <Tile
          icon={AlertTriangle}
          label="At limit"
          value={admission.length}
          sub="ceilings reached"
          amber={admission.length > 0}
        />
      </div>

      <Table
        rows={LIMITS}
        rowKey={(l) => l.key}
        columns={[
          { header: 'Limit', cell: (l) => l.label },
          {
            header: 'Consuming',
            cell: (l) => {
              const cur = l.consuming(used)
              return cur == null ? <span className="dim">—</span> : <Mono>{String(cur)}</Mono>
            },
          },
          {
            header: 'Ceiling',
            cell: (l) => {
              // An UNSET ceiling is unlimited, not zero.
              const lim = l.ceiling(policy)
              return lim == null ? <span className="dim">unlimited</span> : <Mono>{String(lim)}</Mono>
            },
          },
          {
            header: 'Status',
            cell: (l) => {
              const lim = l.ceiling(policy)
              const cur = l.consuming(used)
              // No ceiling means nothing to be at, so there is no verdict to render —
              // an unlimited row must never read as a breach.
              if (lim == null) return <span className="dim">—</span>
              const at = cur != null && Number(cur) >= Number(lim)
              return at ? <Verdict tone="fail">at limit</Verdict> : <Verdict tone="ok">within limit</Verdict>
            },
          },
        ]}
      />

      <SecRow left="Billable totals" right="from the durable meter, not from run traces" />
      {meter ? (
        <Table
          rows={[
            { key: 'calls', label: 'Model calls', value: num(meter.calls) },
            { key: 'inputTokens', label: 'Input tokens', value: num(meter.inputTokens) },
            { key: 'outputTokens', label: 'Output tokens', value: num(meter.outputTokens) },
            { key: 'costUsd', label: 'Cost', value: `$${Number(meter.costUsd ?? 0).toFixed(4)}` },
          ]}
          rowKey={(r) => r.key}
          columns={[
            { header: 'Metric', cell: (r) => r.label },
            { header: 'Value', cell: (r) => <Mono>{r.value}</Mono> },
          ]}
        />
      ) : (
        <Empty icon={Coins}>No metered usage yet.</Empty>
      )}

      <OrgBudget org={quotas.org} />

      {posture && <TenantPosture posture={posture} />}
    </>
  )
}
