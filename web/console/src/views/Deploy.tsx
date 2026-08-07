import { useState } from 'react'
import {
  BadgeCheck, CheckCircle2, Cpu, FileText, Fingerprint, FlaskConical,
  GitBranch, GitCommitVertical, History, ListChecks, Moon, Package, ScanLine,
  ShieldAlert, ShieldHalf, Timer, Unplug, UserCheck,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { absolute, num } from '../lib/format'
import type { ConsoleState, Run } from '../lib/types'
import { Ago, CopyId, Empty, SecRow, StatusBadge, Table, Tile, ViewHeader } from '../components/ui'

// ---- Deploy: workspace -> agent -> environment -> version -> runs ------------
//
// PLATFORM_DESIGN §11 item 12, ported from the legacy console's `loadEnvironments`
// / `openEnvironment` / `loadVersions` / `openVersion` / `loadWorkers`. Three views
// but ONE drill-down, which is why they share a file: an environment names a
// version, a version names its workers and its runs, and the retained-version list
// on an environment opens the same version panel the Versions view opens.
//
// Two rules hold everywhere below, both lifted verbatim from the legacy comments:
// (1) every panel must be CALM when empty — a fresh install has no versions, no
// environments and no workers, and under D12/§6 all three are ordinary states, not
// faults; (2) nothing is fetched across workspaces — every route here resolves
// through the workspace-scoped engine dependency (D13), so the console simply
// cannot see another tenant's deployments.
//
// These views load on ENTRY (`useLoad`), never from the shell's 6s poll:
// deployment topology moves on a promote, not per second.

// ---- response shapes --------------------------------------------------------
// Declared here rather than in lib/types.ts: they are the deploy endpoints'
// payloads and nothing outside this drill-down reads them.

/** `GET /agents/{a}/environments` — the raw POINTER record, and nothing more. */
interface EnvPointer {
  name: string
  agent?: string
  currentVersionId?: string | null
  updatedAt?: string | null
  actor?: string | null
}

/** A row of the version ledger (`GET /versions/{id}`, `GET /agents/{a}/versions`). */
interface VersionRecord {
  id: string
  agent?: string
  bundleHash?: string | null
  manifestVersion?: string | null
  sdkVersion?: string | null
  state?: string | null
  entrypoint?: string | null
  lockfile?: string | null
  fileCount?: number
  sizeBytes?: number
  createdAt?: string | null
  createdBy?: string | null
  retiredAt?: string | null
  metadata?: Record<string, unknown> | null
}

/**
 * `GET /agents/{a}/environments/{env}` (`describe_environment`).
 *
 * `pinnedRuns` maps an OLDER version id to how many runs are still pinned to it —
 * §9's drain step, and the number that answers "can I retire anything yet?".
 */
interface EnvDescription {
  name?: string
  agent?: string
  currentVersionId?: string | null
  currentVersion?: VersionRecord | null
  updatedAt?: string | null
  actor?: string | null
  historyDepth?: number
  pinnedRuns?: Record<string, number>
}

/** One resolved promotion gate (`GET /agents/{a}/gate`). */
interface Gate {
  environment?: string
  enforced?: boolean
  source?: string
  requireReadiness?: boolean
  requireEvals?: boolean
  minEvalScore?: number | null
  requireActor?: boolean
}

/** `GET /agents/{a}/gate/check?env=` — the dry run: what a promotion would refuse. */
interface GateCheck {
  allowed?: boolean
  versionId?: string
  checks?: { check: string; ok: boolean; detail?: string | null; fix?: string | null }[]
}

/** One promote/rollback, newest first (`.../environments/{env}/history`). */
interface HistoryEntry {
  versionId: string
  bundleHash?: string | null
  state?: string | null
  current?: boolean
  at?: string | null
  actor?: string | null
}

/** `GET /versions/{id}/runs` — every run pinned to a version, terminal ones included. */
interface VersionRun extends Run {
  environment?: string | null
  pinned?: boolean
}
interface VersionRuns {
  runs?: VersionRun[]
  count?: number
  /** Non-terminal only: the runs actually holding the version open. */
  pinnedCount?: number
}

/** The promotion gate's evidence, filed against one exact content hash. */
interface Attestation {
  id?: string
  kind: string
  ok?: boolean
  actor?: string | null
  createdAt?: string | null
  detail?: unknown
  hasEvals?: boolean
  passed?: number
  total?: number
  score?: number | null
  failedCases?: string[]
  blocks?: number
  warnings?: number
  blockCodes?: (string | null)[]
  bypassed?: string[]
  environment?: string | null
}

/** `GET /workers` — one execution-plane process (§6). */
interface Worker {
  id: string
  status?: string
  versionId?: string | null
  bundleHash?: string | null
  handlers?: string[]
  host?: string | null
  pid?: number | null
  coldStartMs?: number | null
  lastHeartbeatAt?: string | null
  stats?: { claimed?: number; completed?: number }
}

// ---- shared presentation helpers -------------------------------------------

/** The legacy `short()`: an identity is a content hash, so show enough of it to read. */
const short = (h?: string | null, n = 12) => (h ? String(h).slice(0, n) : '—')

/**
 * The legacy `deployErr()`. An un-authenticated console is not an outage, so a 401
 * asks for a key by name instead of reporting the deployment broken.
 */
function DeployError({ message, what }: { message: string; what: string }) {
  return (
    <Empty icon={Package}>
      {message === 'unauthorized'
        ? `Connect with a workspace key to load ${what}.`
        : `${what} unavailable — ${message}`}
    </Empty>
  )
}

type SpecRow = [key: string, value: ReactNode, tone?: 'ok' | 'dim']

/** The legacy `spec()` card: a titled list of key/value rows inside an `.ispec` grid. */
function SpecCard({ icon: Icon, title, rows }: { icon: LucideIcon; title: string; rows: SpecRow[] }) {
  return (
    <div className="ispc">
      <div className="hd">
        <Icon aria-hidden="true" focusable="false" />
        <span className="t">{title}</span>
      </div>
      {rows.map(([k, v, tone]) => (
        <div className="rw" key={k}>
          <span className="k">{k}</span>
          <span className={`v${tone ? ' ' + tone : ''}`}>{v}</span>
        </div>
      ))}
    </div>
  )
}

/** The legacy `crumbs()`: the drill-down's position in workspace -> agent -> ... */
function Crumbs({ items }: { items: { label: string; onClick?: () => void }[] }) {
  return (
    <div className="filters" style={{ margin: '0 0 14px' }}>
      {items.map((it, i) => (
        <span key={`${it.label}-${i}`}>
          {i > 0 && (
            <span className="dim" style={{ fontSize: 12, padding: '0 2px' }}>
              /
            </span>
          )}
          {it.onClick ? (
            <button className="fpill" onClick={it.onClick}>
              {it.label}
            </button>
          ) : (
            <span className="fpill on">{it.label}</span>
          )}
        </span>
      ))}
    </div>
  )
}

/** The legacy `verState()`: retired is a dim pill, every other state a neutral one. */
function VerState({ state }: { state?: string | null }) {
  return state === 'retired' ? (
    <span className="pb dis">retired</span>
  ) : (
    <span className="pb allowed">{state || '—'}</span>
  )
}

const wsLabel = (state: ConsoleState) => {
  const w = state.viewer?.workspace
  return w && w !== 'default' ? w : 'workspace'
}

/**
 * The legacy `workerTable()`.
 *
 * The server decides staleness now (`store.WORKER_LOST_SECONDS`). The clock check
 * stays as the fallback for the window between "late" and "declared lost", and
 * because a browser clock may be ahead of the deployment's.
 */
function WorkerTable({ workers, emptyMessage }: { workers: Worker[]; emptyMessage: string }) {
  return (
    <Table
      rows={workers}
      rowKey={(w) => w.id}
      emptyIcon={Moon}
      emptyMessage={emptyMessage}
      columns={[
        {
          header: 'Worker',
          cell: (w) => (
            <>
              <span className="mono">{w.id}</span>
              {w.status !== 'alive' && <span className="pb dis"> {w.status || ''}</span>}
            </>
          ),
        },
        {
          header: 'Version',
          cell: (w) =>
            w.versionId ? (
              <span className="mono dim">{w.versionId}</span>
            ) : (
              // No versionId means `rya dev` against the working tree, not a fault.
              <span className="dim">working tree</span>
            ),
        },
        { header: 'Bundle', cell: (w) => <span className="mono dim">{short(w.bundleHash)}</span> },
        {
          header: 'Handlers',
          cell: (w) =>
            (w.handlers ?? []).length ? (
              (w.handlers ?? []).map((h) => (
                <span className="ptag" key={h}>
                  {h}
                </span>
              ))
            ) : (
              <span className="dim">—</span>
            ),
        },
        {
          header: 'Host',
          cell: (w) => <span className="mono dim">{(w.host || '—') + (w.pid ? ` · ${w.pid}` : '')}</span>,
        },
        {
          header: 'Cold start',
          cell: (w) => (w.coldStartMs != null ? `${w.coldStartMs} ms` : <span className="dim">—</span>),
        },
        {
          header: 'Heartbeat',
          cell: (w) => {
            const stale =
              w.status === 'lost' ||
              (!!w.lastHeartbeatAt && Date.now() - Date.parse(w.lastHeartbeatAt) > 120000)
            return stale ? (
              <span className="stbadge wait">
                <span className="d" />
                <Ago ts={w.lastHeartbeatAt} />
              </span>
            ) : (
              <span className="dim">
                <Ago ts={w.lastHeartbeatAt} />
              </span>
            )
          },
        },
      ]}
    />
  )
}

/** Runs pinned to a version. Read-only here: the trace lives in the Runs view. */
function VersionRunsTable({
  runs,
  withEnvironment,
  emptyMessage,
  onToast,
}: {
  runs: VersionRun[]
  withEnvironment?: boolean
  emptyMessage: string
  onToast: (m: string) => void
}) {
  return (
    <Table
      rows={runs}
      rowKey={(r) => r.id}
      emptyIcon={ScanLine}
      emptyMessage={emptyMessage}
      columns={[
        { header: 'Run', cell: (r) => <CopyId id={r.id} onCopied={onToast} /> },
        { header: 'Trigger', cell: (r) => r.trigger || '—' },
        { header: 'Status', cell: (r) => <StatusBadge status={r.status} /> },
        ...(withEnvironment
          ? [
              {
                header: 'Environment',
                cell: (r: VersionRun) =>
                  r.environment ? <span className="mono dim">{r.environment}</span> : <span className="dim">—</span>,
              },
            ]
          : []),
        { header: 'Tokens', cell: (r) => num(r.tokens) },
        { header: 'When', cell: (r) => <Ago ts={r.createdAt} />, className: 'dim' },
      ]}
    />
  )
}

// =============================================================================
// Version detail (`openVersion`)
// =============================================================================

const ATT_ICON: Record<string, LucideIcon> = {
  readiness: ListChecks,
  evals: FlaskConical,
  override: ShieldAlert,
}

/** The legacy `attDetail()`: each attestation kind summarises itself differently. */
function attDetail(a: Attestation): string {
  if (a.kind === 'evals') {
    const head =
      a.hasEvals === false
        ? 'no eval suite'
        : `${a.passed || 0}/${a.total || 0} passed · score ${a.score == null ? '—' : a.score}`
    return head + ((a.failedCases ?? []).length ? ` · failed: ${(a.failedCases ?? []).join(', ')}` : '')
  }
  if (a.kind === 'readiness') {
    const codes = (a.blockCodes ?? []).filter(Boolean)
    return `${a.blocks || 0} blocker(s), ${a.warnings || 0} warning(s)` + (codes.length ? ` · ${codes.join(', ')}` : '')
  }
  if (a.kind === 'override') {
    return `bypassed ${(a.bypassed ?? []).join(', ') || 'nothing'}` + (a.environment ? ` into ${a.environment}` : '')
  }
  return a.detail ? String(a.detail) : '—'
}

/**
 * A version's identity, evidence, runs and workers.
 *
 * Reached from the Versions table AND from an environment's retained-version and
 * promote/rollback tables — the drill-down is one graph, not two pages, which is
 * why this component is shared rather than owned by `VersionsView`.
 */
function VersionDetail({
  state,
  id,
  onToast,
  onBack,
}: {
  state: ConsoleState
  id: string
  onToast: (m: string) => void
  onBack?: () => void
}) {
  const agent = state.agent.name

  const { data, error, loading } = useLoad(async () => {
    // The version ledger is workspace-scoped and addressed by content id (D28
    // Rule 1: the version row names its own agent), so these four are NOT
    // agent-prefixed — exactly as the legacy console spells them.
    const version = await api<VersionRecord>(`/versions/${encodeURIComponent(id)}`)
    const [att, runs, workers] = await Promise.all([
      api<{ attestations?: Attestation[] }>(`/versions/${encodeURIComponent(id)}/attestations`).catch(() => ({
        attestations: [],
      })),
      api<VersionRuns>(`/versions/${encodeURIComponent(id)}/runs?limit=20`).catch(
        (): VersionRuns => ({ runs: [], count: 0 }),
      ),
      api<{ workers?: Worker[] }>(`/workers?version_id=${encodeURIComponent(id)}`).catch(() => ({ workers: [] })),
    ])
    return { version, att: att.attestations ?? [], runs, workers: workers.workers ?? [] }
  }, [agent, id])

  const head = <SecRow left={`Version · ${id}`} />

  if (error) {
    return (
      <>
        {head}
        <DeployError message={error} what="This version" />
      </>
    )
  }
  if (!data) return <>{head}{loading && <Empty icon={Package}>Loading…</Empty>}</>

  const { version: v, att, runs, workers } = data
  const meta = v.metadata ?? {}
  const metaRows = Object.entries(meta).slice(0, 7)

  return (
    <>
      {head}
      <Crumbs
        items={[
          { label: wsLabel(state) },
          { label: agent },
          ...(onBack ? [{ label: 'versions', onClick: onBack }] : []),
          { label: short(v.bundleHash) },
        ]}
      />

      <div className="ispec">
        {/* D12: the identity IS the bundle hash. `manifestVersion` is an
            author-typed label that nothing branches on, so it is shown as one. */}
        <SpecCard
          icon={Fingerprint}
          title="Identity"
          rows={[
            ['bundle hash', short(v.bundleHash, 24)],
            ['sdk', v.sdkVersion || '—'],
            ['entrypoint', v.entrypoint || '—'],
            ['lockfile', v.lockfile || 'none', v.lockfile ? undefined : 'dim'],
            ['files', String(v.fileCount || 0)],
            ['size', `${((v.sizeBytes || 0) / 1024).toFixed(1)} KB`],
            ['manifest label', v.manifestVersion || '—'],
          ]}
        />
        <SpecCard
          icon={GitCommitVertical}
          title="State"
          rows={[
            ['state', v.state || '—', v.state === 'active' ? 'ok' : 'dim'],
            ['recorded', absolute(v.createdAt) || '—'],
            ['recorded by', v.createdBy || 'unattributed', v.createdBy ? undefined : 'dim'],
            ['retired', absolute(v.retiredAt) || '—', v.retiredAt ? undefined : 'dim'],
            ['runs pinned', String(runs.pinnedCount || 0), runs.pinnedCount ? undefined : 'dim'],
          ]}
        />
        <SpecCard
          icon={BadgeCheck}
          title="Provenance"
          rows={
            metaRows.length
              ? metaRows.map(([k, val]) => [k, String(val)] as SpecRow)
              : [
                  ['metadata', 'none recorded', 'dim'],
                  ['why it matters', 'git sha / CI url / who built it', 'dim'],
                ]
          }
        />
      </div>

      {/* No promote / retire controls, deliberately. The legacy console had none
          either: promotion is a gated, audited, actor-attributed action and it lives
          in `rya promote` / `rya deploy --env` / `rya versions retire`, where the
          actor is a real identity rather than whoever had the tab open. This view
          reports the pointer; it does not move it. */}

      <SecRow left="Attestations" right="the promotion gate's evidence · filed against this exact content" />
      <Table
        // Oldest-first on the wire; newest-first is what an operator reads.
        rows={att.slice().reverse()}
        rowKey={(a) => a.id ?? `${a.kind}@${a.createdAt ?? ''}`}
        emptyIcon={FileText}
        emptyMessage="No attestations. Nothing has been checked against this version — an enforced gate would refuse it."
        columns={[
          {
            header: 'Kind',
            cell: (a) => {
              const Icon = ATT_ICON[a.kind] ?? FileText
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
                  <span className="mono">{a.kind}</span>
                </>
              )
            },
          },
          {
            header: 'Result',
            cell: (a) => (
              <span className={`stbadge ${a.ok ? 'ok' : 'fail'}`}>
                <span className="d" />
                {a.ok ? 'pass' : 'fail'}
              </span>
            ),
          },
          {
            header: 'Actor',
            cell: (a) =>
              a.actor ? <span className="mono dim">{a.actor}</span> : <span className="dim">unattributed</span>,
          },
          { header: 'Detail', cell: (a) => attDetail(a) },
          { header: 'Filed', cell: (a) => <Ago ts={a.createdAt} />, className: 'dim' },
        ]}
      />

      <SecRow
        left="Runs"
        right={`${runs.count || 0} pinned to this version · ${runs.pinnedCount || 0} non-terminal`}
      />
      <VersionRunsTable
        runs={runs.runs ?? []}
        withEnvironment
        onToast={onToast}
        emptyMessage="No runs have been pinned to this version."
      />

      <SecRow left="Workers on this version" right="§6 · one process per (workspace, agent, version)" />
      <WorkerTable
        workers={workers}
        emptyMessage="No process is serving this version — idle keys scale to zero."
      />
    </>
  )
}

// =============================================================================
// Environment detail (`openEnvironment`)
// =============================================================================

/**
 * One environment: what is on it, who put it there, what the gate would refuse
 * today, which older versions are still retained, and the runs on the pointer.
 */
function EnvironmentDetail({
  state,
  name,
  onToast,
  onOpenVersion,
}: {
  state: ConsoleState
  name: string
  onToast: (m: string) => void
  onOpenVersion: (id: string) => void
}) {
  const agent = state.agent.name

  const { data, error, loading } = useLoad(async () => {
    const base = ag(agent, `/environments/${encodeURIComponent(name)}`)
    const d = await api<EnvDescription>(base)
    const v = d.currentVersion ?? null
    const [hist, gate, check, runs] = await Promise.all([
      api<{ history?: HistoryEntry[] }>(`${base}/history`).catch(() => ({ history: [] })),
      // Agent-prefixed, unlike the legacy console: a promotion gate is resolved
      // per (environment, AGENT), so the unprefixed `/gate` answers only through
      // the deprecated Rule 6 fallback and 400s E_AGENT_AMBIGUOUS on the day the
      // workspace serves a second agent. See the report note.
      api<{ gates?: Gate[] }>(ag(agent, `/gate?env=${encodeURIComponent(name)}`)).catch(() => null),
      v ? api<GateCheck>(ag(agent, `/gate/check?env=${encodeURIComponent(name)}`)).catch(() => null) : null,
      v
        ? api<VersionRuns>(`/versions/${encodeURIComponent(v.id)}/runs?limit=10`).catch(
            (): VersionRuns => ({ runs: [], count: 0 }),
          )
        : ({ runs: [], count: 0 } as VersionRuns),
    ])
    // §9's drain step, made visible: which OLDER versions are still retained
    // because a run is pinned to them. This is the least obvious part of D12, so
    // each pinned id is resolved to its record for the hash and state.
    const stale = Object.entries(d.pinnedRuns ?? {})
    const staleRecords = await Promise.all(
      stale.map(([vid]) => api<VersionRecord>(`/versions/${encodeURIComponent(vid)}`).catch(() => null)),
    )
    return {
      d,
      v,
      history: hist.history ?? [],
      gate: (gate?.gates ?? [])[0] ?? null,
      check,
      runs,
      stale: stale.map(([vid, count], i) => ({ id: vid, count, record: staleRecords[i] })),
    }
  }, [agent, name])

  const head = <SecRow left={`Environment · ${name}`} />

  if (error) {
    return (
      <>
        {head}
        <DeployError message={error} what="This environment" />
      </>
    )
  }
  if (!data) return <>{head}{loading && <Empty icon={GitBranch}>Loading…</Empty>}</>

  const { d, v, history, gate: g, check, runs, stale } = data
  const unmet = (check?.checks ?? []).filter((c) => !c.ok)

  return (
    <>
      {head}
      <Crumbs items={[{ label: wsLabel(state) }, { label: agent }, { label: name }]} />

      <div className="ispec">
        <SpecCard
          icon={Package}
          title="Current version"
          rows={
            v
              ? [
                  ['version id', v.id],
                  ['bundle', short(v.bundleHash, 16)],
                  ['manifest label', v.manifestVersion || '—'],
                  ['sdk', v.sdkVersion || '—'],
                  ['state', v.state || '—', v.state === 'active' ? 'ok' : 'dim'],
                ]
              : [
                  ['version id', 'nothing promoted', 'dim'],
                  ['bundle', '—', 'dim'],
                  ['state', 'empty environment', 'dim'],
                ]
          }
        />
        {/* §12 risk 7: "who reviewed this change" is a feature, so the actor is a
            first-class field rather than a line in a log. */}
        <SpecCard
          icon={UserCheck}
          title="Promotion"
          rows={[
            ['promoted by', d.actor || 'unattributed', d.actor ? undefined : 'dim'],
            ['at', absolute(d.updatedAt) || '—'],
            ['prior pointers', String(d.historyDepth || 0)],
            [
              'rollback',
              d.historyDepth ? 'available (pointer flip)' : 'no prior version',
              d.historyDepth ? 'ok' : 'dim',
            ],
          ]}
        />
        <SpecCard
          icon={ShieldHalf}
          title="Promotion gate"
          rows={
            g
              ? [
                  ['enforced', g.enforced ? 'yes' : 'no', g.enforced ? 'ok' : 'dim'],
                  ['source', g.source || '—'],
                  ['readiness', g.requireReadiness ? 'required' : '—', g.requireReadiness ? undefined : 'dim'],
                  [
                    'evals',
                    g.requireEvals ? `required ≥ ${g.minEvalScore}` : '—',
                    g.requireEvals ? undefined : 'dim',
                  ],
                  ['actor', g.requireActor ? 'required' : '—', g.requireActor ? undefined : 'dim'],
                  [
                    'current verdict',
                    check ? (check.allowed ? 'satisfied' : `${unmet.length} unmet`) : '—',
                    check ? (check.allowed ? 'ok' : undefined) : 'dim',
                  ],
                ]
              : [
                  ['enforced', 'unknown', 'dim'],
                  ['source', '—', 'dim'],
                ]
          }
        />
      </div>

      <div className="filters" style={{ margin: '14px 0' }}>
        {/* No rollback control, for the same reason: a rollback is a promotion
          backwards, and it belongs to `rya rollback` where the actor is recorded. */}
      </div>

      {unmet.length > 0 && (
        <>
          <SecRow
            left="Unmet requirements"
            right={`what a promotion into ${name} would refuse today`}
          />
          <Table
            rows={unmet}
            rowKey={(c) => c.check}
            columns={[
              { header: 'Check', cell: (c) => <span className="mono">{c.check}</span> },
              { header: 'Detail', cell: (c) => c.detail || '—' },
              { header: 'Fix', cell: (c) => <span className="dim">{c.fix || '—'}</span> },
            ]}
          />
        </>
      )}

      <SecRow
        left="Retained versions"
        right="held open because runs are still pinned to them · §9 drain"
      />
      <Table
        rows={stale}
        rowKey={(s) => s.id}
        onRowClick={(s) => onOpenVersion(s.id)}
        rowLabel={(s) => `Open version ${s.id}`}
        emptyIcon={CheckCircle2}
        emptyMessage={
          v
            ? 'Fully drained — no run is pinned to an older version, so every other version can be retired.'
            : 'Nothing is pinned here yet.'
        }
        columns={[
          { header: 'Version', cell: (s) => <CopyId id={s.id} onCopied={onToast} /> },
          { header: 'Bundle', cell: (s) => <span className="mono dim">{short(s.record?.bundleHash)}</span> },
          { header: 'State', cell: (s) => <VerState state={s.record?.state} /> },
          { header: 'Pinned runs', cell: (s) => String(s.count) },
          {
            header: 'Retirement',
            cell: () => (
              <span className="stbadge wait">
                <span className="d" />
                blocked
              </span>
            ),
          },
        ]}
      />

      <SecRow
        left="Promote / rollback history"
        right="newest first · a rollback is the same pointer flip backwards"
      />
      <Table
        rows={history}
        // A version id can appear MORE than once here (promote A, promote B, roll
        // back to A), so the pointer timestamp is part of the key.
        rowKey={(h) => `${h.versionId}@${h.at ?? ''}`}
        onRowClick={(h) => onOpenVersion(h.versionId)}
        rowLabel={(h) => `Open version ${h.versionId}`}
        emptyIcon={History}
        emptyMessage="No promotions recorded yet."
        columns={[
          { header: 'Version', cell: (h) => <CopyId id={h.versionId} onCopied={onToast} /> },
          { header: 'Bundle', cell: (h) => <span className="mono dim">{short(h.bundleHash)}</span> },
          {
            header: 'Actor',
            cell: (h) =>
              h.actor ? <span className="mono dim">{h.actor}</span> : <span className="dim">unattributed</span>,
          },
          { header: 'When', cell: (h) => <Ago ts={h.at} />, className: 'dim' },
          {
            header: 'Pointer',
            cell: (h) =>
              h.current ? (
                <span className="stbadge ok">
                  <span className="d" />
                  current
                </span>
              ) : (
                <span className="dim">replaced</span>
              ),
          },
        ]}
      />

      <SecRow
        left="Runs on this version"
        right={`${runs.count || 0} total · ${runs.pinnedCount || 0} still holding the version open`}
      />
      <VersionRunsTable
        runs={runs.runs ?? []}
        onToast={onToast}
        emptyMessage={v ? 'No runs on the current version yet.' : 'Promote a version to start running here.'}
      />
    </>
  )
}

// =============================================================================
// Environments (`loadEnvironments`) — nav id `deploy`
// =============================================================================

interface EnvRow {
  env: EnvPointer
  /** `null` when `describe_environment` failed for this one row; the table degrades. */
  desc: EnvDescription | null
  gate?: Gate
}

export function EnvironmentsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const [openEnv, setOpenEnv] = useState<string | null>(null)
  const [openVersion, setOpenVersion] = useState<string | null>(null)
  const agent = state.agent.name

  const { data, error, loading } = useLoad(async () => {
    const envs = (await api<{ environments?: EnvPointer[] }>(ag(agent, '/environments'))).environments ?? []
    if (!envs.length) return { rows: [] as EnvRow[], versions: 0 }

    // The list route returns the raw POINTER record; the bundle hash and the
    // retention story (§9's drain step) only come from describe_environment, so
    // every row costs a second request. That is the shape of the API, not an
    // oversight — `list_environments` is `store.env_list()` and nothing more.
    const desc = await Promise.all(
      envs.map((e) =>
        api<EnvDescription>(ag(agent, `/environments/${encodeURIComponent(e.name)}`)).catch(() => null),
      ),
    )

    // Gate state comes from the gate endpoint, keyed by environment name. A
    // workspace with no gate policy is the ordinary case, so a failure here dims
    // one column rather than failing the view.
    const gates: Record<string, Gate> = {}
    try {
      for (const g of (await api<{ gates?: Gate[] }>(ag(agent, '/gate'))).gates ?? []) {
        if (g.environment) gates[g.environment] = g
      }
    } catch {
      /* no gate policy yet — the column reads "—" */
    }

    // The legacy tile read a cross-view cache (`DEPLOY.versions`) and showed "—"
    // until the Versions page had been visited, which made it lie about a fresh
    // install. One extra read is cheaper than a wrong number.
    let versions = 0
    try {
      versions = ((await api<{ versions?: VersionRecord[] }>(ag(agent, '/versions'))).versions ?? []).length
    } catch {
      /* the tile falls back to 0 */
    }

    return {
      rows: envs.map((env, i): EnvRow => ({ env, desc: desc[i] ?? null, gate: gates[env.name] })),
      versions,
    }
  }, [agent])

  const header = (
    <ViewHeader title="Environments">
      An environment is a <strong>pointer</strong> to one immutable, content-hashed version. Promote flips
      it, rollback flips it back, and in-flight runs finish on the version they pinned.
    </ViewHeader>
  )

  if (error) {
    return (
      <>
        {header}
        <DeployError message={error} what="Environments" />
      </>
    )
  }
  if (!data) {
    return (
      <>
        {header}
        {loading && <Empty icon={GitBranch}>Loading environments…</Empty>}
      </>
    )
  }

  const { rows } = data
  if (!rows.length) {
    return (
      <>
        {header}
        <Empty icon={GitBranch}>
          No environments yet. An environment comes into existence with its first promotion — rya deploy
          --env prod.
        </Empty>
      </>
    )
  }

  const promoted = rows.filter((r) => r.desc?.currentVersion).length
  const retained = rows.reduce((n, r) => n + Object.keys(r.desc?.pinnedRuns ?? {}).length, 0)
  const gated = rows.filter((r) => r.gate?.enforced).length

  return (
    <>
      {header}

      <div className="stats" style={{ marginBottom: 20 }}>
        <Tile icon={GitBranch} label="Environments" value={rows.length} sub={`${promoted} with a version`} />
        <Tile icon={Package} label="Versions" value={data.versions || '—'} sub="recorded for this agent" />
        <Tile icon={ShieldHalf} label="Gated" value={gated} sub="require evidence to promote" />
        <Tile
          icon={History}
          label="Retained"
          value={retained}
          sub="older versions still pinned"
          amber={retained > 0}
        />
      </div>

      <Table
        rows={rows}
        rowKey={(r) => r.env.name}
        onRowClick={(r) => {
          setOpenEnv(r.env.name)
          setOpenVersion(null)
        }}
        rowLabel={(r) => `Open environment ${r.env.name}`}
        columns={[
          { header: 'Environment', cell: (r) => <span className="mono">{r.env.name}</span> },
          {
            header: 'Current version',
            cell: (r) =>
              r.desc?.currentVersion ? (
                <CopyId id={r.desc.currentVersion.id} onCopied={onToast} />
              ) : (
                <span className="dim">nothing promoted</span>
              ),
          },
          {
            header: 'Bundle',
            cell: (r) =>
              r.desc?.currentVersion ? (
                <span className="mono dim">{short(r.desc.currentVersion.bundleHash)}</span>
              ) : (
                <span className="dim">—</span>
              ),
          },
          {
            header: 'Promoted by',
            cell: (r) =>
              r.desc?.actor ? (
                <span className="mono dim">{r.desc.actor}</span>
              ) : (
                <span className="dim">unattributed</span>
              ),
          },
          { header: 'Promoted', cell: (r) => <Ago ts={r.desc?.updatedAt} />, className: 'dim' },
          {
            header: 'Gate',
            cell: (r) =>
              r.gate ? (
                r.gate.enforced ? (
                  <span className="pb appr">gated</span>
                ) : (
                  <span className="pb allowed">open</span>
                )
              ) : (
                <span className="dim">—</span>
              ),
          },
          {
            header: 'Retention',
            cell: (r) => {
              const n = Object.keys(r.desc?.pinnedRuns ?? {}).length
              return n ? <span className="pb appr">{n} pinned</span> : <span className="dim">drained</span>
            },
          },
        ]}
      />

      {openEnv && (
        <EnvironmentDetail
          state={state}
          name={openEnv}
          onToast={onToast}
          onOpenVersion={setOpenVersion}
        />
      )}

      {/* The retained-version and history tables drill into the SAME version panel
          the Versions view shows, rendered inline: the hierarchy is one graph, and
          a view cannot navigate the shell from here. */}
      {openVersion && (
        <VersionDetail
          state={state}
          id={openVersion}
          onToast={onToast}
          onBack={() => setOpenVersion(null)}
        />
      )}
    </>
  )
}

// =============================================================================
// Versions (`loadVersions`) — nav id `versions`
// =============================================================================

export function VersionsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const [open, setOpen] = useState<string | null>(null)
  const agent = state.agent.name

  const { data, error, loading } = useLoad(async () => {
    const versions = (await api<{ versions?: VersionRecord[] }>(ag(agent, '/versions'))).versions ?? []
    if (!versions.length) return { versions, pointers: {} as Record<string, string[]>, envs: [] as string[] }
    // Which environments point AT each version — the inverse of the environment
    // table, and the column that answers "is this hash live anywhere?".
    let envs: EnvPointer[] = []
    try {
      envs = (await api<{ environments?: EnvPointer[] }>(ag(agent, '/environments'))).environments ?? []
    } catch {
      /* the "Deployed to" column reads "—" */
    }
    const pointers: Record<string, string[]> = {}
    for (const e of envs) {
      if (e.currentVersionId) (pointers[e.currentVersionId] ??= []).push(e.name)
    }
    return { versions, pointers, envs: envs.map((e) => e.name) }
  }, [agent])

  const header = (
    <ViewHeader title="Versions">
      <code>rya deploy</code> bundles source + lockfile + manifest + SDK version and records one
      immutable, content-hashed version. Nothing is overwritten.
    </ViewHeader>
  )

  if (error) {
    return (
      <>
        {header}
        <DeployError message={error} what="Versions" />
      </>
    )
  }
  if (!data) {
    return (
      <>
        {header}
        {loading && <Empty icon={Package}>Loading versions…</Empty>}
      </>
    )
  }

  if (!data.versions.length) {
    return (
      <>
        {header}
        <Empty icon={Package}>
          No versions recorded. rya deploy bundles source + lockfile + manifest + SDK version and records
          one immutable, content-hashed version.
        </Empty>
      </>
    )
  }

  return (
    <>
      {header}

      <Table
        rows={data.versions}
        rowKey={(v) => v.id}
        onRowClick={(v) => setOpen(v.id)}
        rowLabel={(v) => `Open version ${v.id}`}
        columns={[
          { header: 'Version', cell: (v) => <CopyId id={v.id} onCopied={onToast} /> },
          { header: 'Bundle', cell: (v) => <span className="mono dim">{short(v.bundleHash)}</span> },
          {
            header: 'Label',
            cell: (v) =>
              v.manifestVersion ? (
                <span className="mono">{v.manifestVersion}</span>
              ) : (
                <span className="dim">—</span>
              ),
          },
          { header: 'SDK', cell: (v) => <span className="mono dim">{v.sdkVersion || '—'}</span> },
          { header: 'State', cell: (v) => <VerState state={v.state} /> },
          {
            header: 'Deployed to',
            cell: (v) => {
              const names = data.pointers[v.id] ?? []
              return names.length ? (
                names.map((n) => (
                  <span className="ptag" key={n}>
                    {n}
                  </span>
                ))
              ) : (
                <span className="dim">—</span>
              )
            },
          },
          { header: 'Recorded', cell: (v) => <Ago ts={v.createdAt} />, className: 'dim' },
        ]}
      />

      {open && (
        <VersionDetail
          state={state}
          id={open}
          onToast={onToast}
          onBack={() => setOpen(null)}
        />
      )}
    </>
  )
}

// =============================================================================
// Workers (`loadWorkers`) — nav id `workers`
// =============================================================================

export function WorkersView({
  state,
}: {
  state: ConsoleState
  // Part of the shared view signature. Workers is read-only, so nothing here
  // toasts — but a caller must not have to know which of the three does.
  onToast: (m: string) => void
}) {
  // Workspace-scoped, NOT agent-prefixed: `/workers` lists the execution-plane
  // processes of this workspace and each row names its own agent and version.
  // There is no `/agents/{a}/workers` route to prefix to.
  //
  // `?status=` (empty) asks for EVERY status rather than the `alive` default. An
  // empty list is scale-to-zero (§6) — the DESIGNED idle state for an idle key —
  // so this view must read as "idle", never as an outage. Which is exactly why a
  // crashed worker must not be filtered out: dropping it would empty the list and
  // make a crash look identical to scale-to-zero. Since Phase 3 the server derives
  // `status` from heartbeat age, so a SIGKILLed process comes back `lost` and gets
  // its own tile instead of vanishing.
  const { data, error, loading } = useLoad(
    () => api<{ workers?: Worker[] }>('/workers?status='),
    [state.agent.name],
  )

  const header = (
    <ViewHeader title="Workers">
      The execution plane: one process per (workspace, agent, version), claiming from the durable
      queue. Idle keys scale to zero.
    </ViewHeader>
  )

  if (error) {
    return (
      <>
        {header}
        <DeployError message={error} what="Workers" />
      </>
    )
  }
  if (!data) {
    return (
      <>
        {header}
        {loading && <Empty icon={Cpu}>Loading workers…</Empty>}
      </>
    )
  }

  const list = data.workers ?? []
  const live = list.filter((w) => w.status === 'alive')
  const lost = list.filter((w) => w.status === 'lost')
  const cold = live.map((w) => w.coldStartMs || 0).filter(Boolean)
  const done = list.reduce((n, w) => n + (w.stats?.completed ?? 0), 0)
  const claimed = list.reduce((n, w) => n + (w.stats?.claimed ?? 0), 0)

  return (
    <>
      {header}

      <div className="stats" style={{ marginBottom: 20 }}>
        <Tile
          icon={Cpu}
          label="Live processes"
          value={live.length}
          sub={live.length ? 'claiming from the queue' : 'scaled to zero'}
        />
        <Tile
          icon={Package}
          label="Versions served"
          value={new Set(live.map((w) => w.versionId || 'local')).size}
          sub="one process per version"
        />
        {/* A lost worker is worth its own tile: the supervisor replaces it, and an
            operator needs to see that it happened rather than infer it from a
            shrinking list. With none lost, the cold-start number is the useful one. */}
        {lost.length ? (
          <Tile
            icon={Unplug}
            label="Lost"
            value={lost.length}
            sub="stopped heartbeating; the supervisor replaces these"
            amber
          />
        ) : (
          <Tile
            icon={Timer}
            label="Slowest cold start"
            value={cold.length ? `${Math.max(...cold)} ms` : '—'}
            sub="on the critical path after idle"
          />
        )}
        <Tile icon={CheckCircle2} label="Work done" value={num(done)} sub={`${num(claimed)} claimed this uptime`} />
      </div>

      <WorkerTable
        workers={list}
        emptyMessage="No workers running. That is the idle steady state: a process with no claimed work and an empty queue exits, and the next run pays a cold start."
      />
    </>
  )
}
