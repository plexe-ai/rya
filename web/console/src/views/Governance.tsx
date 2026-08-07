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
    // `snapshot.py` derives this from `jwt_configured()` — RYA_JWT_SECRET or
    // RYA_JWKS_URL being set. That is "the mechanism is configured", NOT "every run
    // carries an identity": a caller that presents no user token still produces an
    // anonymous, workspace-shared run. The old tip claimed the stronger thing.
    name: 'Per-user identity',
    tip: 'JWT verification configured — runs that present one are bound to a user',
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

/** Where the guard in force came from, in one word. `snapshot.py` sends `store`,
 *  `file:<path>` or `none`; the path is noise on a dashboard, the distinction is not. */
function origin(source?: string): string {
  if (!source || source === 'none') return ''
  return source.startsWith('file:') ? 'project file' : source
}

/** The Egress rules tile's caption — four genuinely different states, and only one
 *  of them is "default deny". `not configured` used to cover all four. */
function egressSub(p: Governance['policy']): string {
  if (p.egressError) return 'unreadable — denying everything'
  if (p.egressSource === 'none') return 'no policy — not enforced'
  if (!p.egressDefault) return 'not configured'
  const from = origin(p.egressSource)
  return `default ${p.egressDefault}${from ? ` · from ${from}` : ''}`
}

/**
 * Governance.
 *
 * Enforced in the runtime: blocks, not warnings. Every panel degrades calmly — no kill
 * switches and no violations are the ordinary state of a healthy deployment, not a gap
 * in the data.
 *
 * The corollary, and the reason for the `.anote` bands below: a panel that CANNOT read
 * its source must not degrade calmly, because "no overrides" and "we could not ask" are
 * the same picture and opposite facts. This view reported the first while meaning the
 * second for both of its data sources — see `snapshot._governance` and audit §4.5.
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
  const overridden = policy.toolsOverridden ?? 0

  return (
    <>
      {header}

      <SecRow left="Enforcement" />
      <div className="stats" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))' }}>
        {GATES.map((gate) => (
          <Gate key={gate.name} name={gate.name} on={gate.of(g.enforcement)} tip={gate.tip} />
        ))}
      </div>

      <SecRow
        left="Policy"
        right={
          <>
            {/* Only shown when true, and it is worth showing: it tells the reader the
                counts beside it are the EFFECTIVE permissions, not the manifest's. */}
            {overridden > 0 && `${overridden} override${overridden === 1 ? '' : 's'} · `}
            <Mono>{policy.hash}</Mono>
          </>
        }
      />
      {policy.egressError && (
        <div className="anote" role="status">
          The egress policy could not be read — <Mono>{policy.egressError}</Mono>. The guard
          fails closed, so every outbound request is being denied.
        </div>
      )}
      <div className="stats" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(130px,1fr))' }}>
        <Tile icon={UserCheck} label="Gated" value={policy.toolsGated} sub="approval required" />
        <Tile icon={Ban} label="Denied" value={policy.toolsDenied} sub="blocked" />
        <Tile icon={Pin} label="Pinned args" value={policy.pinnedArgTools} sub="server-set" />
        <Tile
          icon={ShieldHalf}
          label="Egress rules"
          value={policy.egressRules}
          sub={egressSub(policy)}
          amber={!!policy.egressError}
        />
      </div>

      <SecRow
        left="Kill switches"
        right={`${switches.version ? `v${switches.version} · ` : ''}immediate · no redeploy`}
      />
      {switches.error ? (
        // NOT an empty table. "No overrides." here would be the §4.5 failure again,
        // one layer up: an unreadable policy store reported as a governed state.
        <div className="anote" role="status">
          The policy store could not be read — <Mono>{switches.error}</Mono>. Overrides and
          their history are unknown, not absent.
        </div>
      ) : (
        <Table
          rows={switches.active}
          // One override per tool, so the tool id is the natural key.
          rowKey={(o) => o.tool}
          emptyMessage="No overrides."
          columns={[
            { header: 'Tool', cell: (o) => <Mono>{o.tool}</Mono> },
            {
              header: 'Now',
              cell: (o) => (
                <span className={`pb ${PERM_CLASS[o.permission] ?? ''}`}>{o.permission}</span>
              ),
            },
            { header: 'Since', cell: (o) => stamp(o.ts) },
            // Was `v{o.version}`, which rendered `vundefined`: the policy log versions
            // the whole switches map, so there is no per-tool version and never was.
            // The document version is in the header; the reason is what this row can
            // actually answer.
            { header: 'Reason', cell: (o) => o.reason || '' },
          ]}
        />
      )}

      {switches.history.length > 0 && (
        <>
          <SecRow left="History" right="append-only" />
          <Table
            rows={switches.history}
            // Derived from document snapshots, so one policy version can yield several
            // rows — the version alone is not unique. Tool plus transition is.
            rowKey={(h) => `${h.version ?? h.ts ?? ''}·${h.tool}·${h.previous ?? ''}>${h.permission ?? ''}`}
            columns={[
              { header: 'When', cell: (h) => stamp(h.ts) },
              { header: 'Tool', cell: (h) => <Mono>{h.tool}</Mono> },
              {
                header: 'Change',
                cell: (h) =>
                  h.cleared
                    ? `cleared${h.permission ? ` — back to ${h.permission}` : ''}`
                    : `${h.previous ?? 'declared'} -> ${h.permission}`,
              },
              // §12 risk 7, quoted in store.py: "who reviewed this allowlist change" is
              // the feature. `actor` was written on every record and shown on no screen.
              { header: 'Who', cell: (h) => h.actor || '—' },
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
