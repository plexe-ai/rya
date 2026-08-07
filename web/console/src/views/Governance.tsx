import { Ban, Pin, ShieldCheck, ShieldHalf, ShieldOff, UserCheck } from 'lucide-react'
import { PERM_CLASS, stamp } from '../lib/format'
import type { ConsoleState, Governance } from '../lib/types'
import { Empty, Mono, SecRow, Table, Tile, ViewHeader } from '../components/ui'

// No response interfaces here: governance arrives inside the `/console` aggregate the
// shell already polls, so this view is a pure function of `state` and fetches nothing.
// `Governance` is typed in lib/types.ts.

/**
 * One enforcement gate. Not `Tile`: the value is a word rather than a figure, so it
 * carries the smaller type and the green/amber colour the legacy `gate()` used, and the
 * `title` is what tells an operator what the gate actually checks.
 */
function Gate({ name, on, tip }: { name: string; on: boolean; tip: string }) {
  const Icon = on ? ShieldCheck : ShieldOff
  return (
    <div className="stat" title={tip}>
      <div className="k">
        <Icon aria-hidden="true" focusable="false" />
        {name}
      </div>
      <div className="v" style={{ fontSize: 15, color: on ? 'var(--green)' : 'var(--amber)' }}>
        {on ? 'enforced' : 'off'}
      </div>
    </div>
  )
}

/** The six gates, each with the one-line description of what it checks. */
const GATES: readonly {
  name: string
  tip: string
  of: (e: Governance['enforcement']) => boolean
}[] = [
  {
    name: 'Egress guard',
    tip: 'every outbound request checked before it leaves the process',
    of: (e) => e.egressGuard,
  },
  {
    name: 'Grounding gate',
    tip: 'outbound figures must trace to tool outputs',
    of: (e) => e.groundingGate,
  },
  {
    name: 'Approver identity',
    tip: 'approvals must carry a verified user',
    of: (e) => e.approverIdentity,
  },
  {
    name: 'Per-user identity',
    tip: 'verified JWT bound to every run',
    of: (e) => e.perUserIdentity,
  },
  {
    name: 'Tenant isolation',
    tip: 'Postgres row-level security per workspace',
    of: (e) => e.multiTenantRls,
  },
  {
    name: 'Secrets sealed',
    tip: 'encrypted at rest, redacted in traces',
    of: (e) => e.secretsSealed,
  },
]

/**
 * Governance.
 *
 * Enforced in the runtime: blocks, not warnings. Every panel degrades calmly — no kill
 * switches and no violations are the ordinary state of a healthy deployment, not a gap
 * in the data.
 */
export function GovernanceView({
  state,
  onToast: _onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const g = state.governance

  const header = (
    <ViewHeader title="Governance">Enforced in the runtime. Blocks, not warnings.</ViewHeader>
  )

  if (!g) {
    return (
      <>
        {header}
        <Empty>Unavailable.</Empty>
      </>
    )
  }

  const policy = g.policy
  const switches = g.switches ?? { active: [], history: [] }
  const violations = g.violations ?? []

  return (
    <>
      {header}

      <SecRow left="Enforcement" />
      <div className="stats" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))' }}>
        {GATES.map((gate) => (
          <Gate key={gate.name} name={gate.name} on={gate.of(g.enforcement)} tip={gate.tip} />
        ))}
      </div>

      <SecRow left="Policy" right={<Mono>{policy.hash}</Mono>} />
      <div className="stats" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(130px,1fr))' }}>
        <Tile icon={UserCheck} label="Gated" value={policy.toolsGated} sub="approval required" />
        <Tile icon={Ban} label="Denied" value={policy.toolsDenied} sub="blocked" />
        <Tile icon={Pin} label="Pinned args" value={policy.pinnedArgTools} sub="server-set" />
        <Tile
          icon={ShieldHalf}
          label="Egress rules"
          value={policy.egressRules}
          sub={policy.egressDefault ? `default ${policy.egressDefault}` : 'not configured'}
        />
      </div>

      <SecRow left="Kill switches" right="immediate · no redeploy" />
      <Table
        rows={switches.active}
        // One override per tool, so the tool id is the natural key.
        rowKey={(o) => o.tool}
        emptyMessage="No overrides."
        columns={[
          { header: 'Tool', cell: (o) => <Mono>{o.tool}</Mono> },
          {
            header: 'Now',
            cell: (o) => <span className={`pb ${PERM_CLASS[o.permission] ?? ''}`}>{o.permission}</span>,
          },
          { header: 'Since', cell: (o) => stamp(o.ts) },
          { header: 'v', cell: (o) => `v${o.version}` },
        ]}
      />

      {switches.history.length > 0 && (
        <>
          <SecRow left="History" right="append-only" />
          <Table
            rows={switches.history}
            // The log is append-only, so an entry is identified by when it was written
            // and to which tool, plus the transition it recorded.
            rowKey={(h) => `${h.ts ?? ''}·${h.tool}·${h.previous ?? ''}>${h.permission}`}
            columns={[
              { header: 'When', cell: (h) => stamp(h.ts) },
              { header: 'Tool', cell: (h) => <Mono>{h.tool}</Mono> },
              {
                header: 'Change',
                cell: (h) => (h.cleared ? `restored ${h.permission}` : `${h.previous} -> ${h.permission}`),
              },
              { header: 'Reason', cell: (h) => h.reason || '' },
            ]}
          />
        </>
      )}

      <SecRow left="Violations" right="blocked by the runtime" />
      <Table
        rows={violations}
        rowKey={(v) => `${v.ts ?? ''}·${v.runId ?? ''}·${v.kind}`}
        emptyMessage="None recorded."
        columns={[
          { header: 'When', cell: (v) => stamp(v.ts) },
          { header: 'What', cell: (v) => <Mono>{v.kind}</Mono> },
          { header: 'Run', cell: (v) => <Mono>{(v.runId ?? '').slice(0, 14)}</Mono> },
          { header: 'Detail', cell: (v) => v.detail || '' },
        ]}
      />
    </>
  )
}
