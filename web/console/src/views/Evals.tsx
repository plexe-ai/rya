import { useState } from 'react'
import { FlaskConical, Play } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import type { ConsoleState } from '../lib/types'
import { CopyId, Empty, Mono, SecRow, StatusBadge, Table, ViewHeader } from '../components/ui'

// Ported from the legacy console's `loadEvals` (index.html:864-876) and `runEvals`
// (877-893).
//
// Why this view earns its place, stated plainly: evals are the only BEHAVIOURAL
// evidence a promotion gate can require. Everything else a gate checks is
// structural — a bundle hash, a lockfile, who promoted what. That is why a missing
// `rya.evals.yaml` renders as a warning rather than as nothing: "no suite" and "a
// passing suite" are different states, and a gate that cannot tell them apart lets
// an unproven bundle through while looking green.

/** One declared case from `rya.evals.yaml` (`evals.py: load_evals`). */
interface EvalCase {
  id: string
  trigger?: { type?: string; payload?: Record<string, unknown> } | null
  expect?: Record<string, unknown> | null
}

/**
 * `GET /agents/{agent}/evals`.
 *
 * `exists: false` is ordinary and arrives two ways: the project has no
 * `rya.evals.yaml`, or this api process does not have that agent's project tree
 * mounted at all (cases live in the tree, so the route says so in `note`).
 */
interface EvalsResponse {
  agent?: string
  cases?: EvalCase[]
  exists?: boolean
  note?: string
}

/** One scorer verdict — `evals.py: _score_expect`. */
interface EvalCheck {
  check: string
  pass: boolean
  detail?: string
  value?: number
}

interface EvalCaseResult {
  id: string
  pass: boolean
  runId?: string | null
  status?: string | null
  checks?: EvalCheck[]
  error?: string | null
}

/** `POST /agents/{agent}/evals/run` — `evals.py: run_evals`. */
interface EvalRunResponse {
  ok: boolean
  total: number
  passed: number
  failed: number
  score?: number | null
  results?: EvalCaseResult[]
  hasEvals?: boolean
}

export function EvalsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  // Loads on entry, not from the shell's 6s poll: a declared eval suite changes
  // when someone edits a file, not per second (console/AGENTS.md).
  const agent = state.agent.name
  const { data, error, loading } = useLoad<EvalsResponse>(
    () => api<EvalsResponse>(ag(agent, '/evals')),
    [agent],
  )

  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<EvalRunResponse | null>(null)

  async function runEvals() {
    setRunning(true)
    try {
      // POST, and agent-prefixed like the GET: `rya eval` runs server-side, and the
      // unprefixed `/evals/run` resolves the reserved `_` alias, which 400s
      // `E_AGENT_AMBIGUOUS` as soon as the workspace serves a second agent.
      const r = await api<EvalRunResponse>(ag(agent, '/evals/run'), { method: 'POST' })
      setResult(r)
      onToast(r.ok ? `All evals passed (${r.passed}/${r.total})` : `${r.failed} eval(s) failed`)
    } catch (e) {
      // A 409 `E_NO_INLINE_WORKER` lands here: this api process cannot import the
      // agent, so it cannot run its evals. The server's message names the fix
      // (`rya eval` in the project, or a readiness attestation in CI), so surface
      // it verbatim rather than translating it into "failed".
      onToast(`Eval run failed — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRunning(false)
    }
  }

  const cases = data?.cases ?? []

  return (
    <>
      <ViewHeader title="Evals">
        Behavioural checks — each case fires a real event and scores the run. Gate a deploy on
        it with <span className="mono">rya eval</span>.
      </ViewHeader>

      <div style={{ marginBottom: 16 }}>
        <button className="btn dark sm" onClick={() => void runEvals()} disabled={running}>
          <Play aria-hidden="true" focusable="false" />
          {running ? 'Running evals…' : 'Run evals'}
        </button>
      </div>

      {loading && !data && !error ? (
        <Empty icon={FlaskConical}>Loading cases…</Empty>
      ) : error ? (
        <Empty icon={FlaskConical}>
          {error === 'unauthorized'
            ? 'Connect with an operator token to load evals.'
            : `Evals unavailable — ${error}`}
        </Empty>
      ) : data?.exists === false ? (
        // Not an error: a project without a suite is a normal project. Say what is
        // missing and how to get one, because the gate will ask for it later.
        <Empty icon={FlaskConical}>
          {data.note ?? 'No rya.evals.yaml — scaffold one with rya create, or add cases.'}
        </Empty>
      ) : (
        <>
          <SecRow left="Cases" right={`${cases.length} declared`} />
          <Table
            rows={cases}
            rowKey={(c) => c.id}
            emptyIcon={FlaskConical}
            emptyMessage="No cases."
            columns={[
              { header: 'Case', cell: (c) => <Mono>{c.id}</Mono> },
              { header: 'Trigger', cell: (c) => <Mono className="dim">{c.trigger?.type ?? '—'}</Mono> },
              {
                header: 'Expectations',
                cell: (c) =>
                  Object.keys(c.expect ?? {}).map((k) => (
                    <span className="ptag" key={k}>
                      {k}
                    </span>
                  )),
              },
            ]}
          />
        </>
      )}

      {result && <RunResult result={result} onToast={onToast} />}
    </>
  )
}

/**
 * The last run's verdict. Kept below the declared cases rather than replacing them
 * (the legacy view overwrote the case list with the results, which loses the
 * denominator: "3/3 passed" reads very differently next to a suite of ten).
 */
function RunResult({
  result,
  onToast,
}: {
  result: EvalRunResponse
  onToast: (m: string) => void
}) {
  const results = result.results ?? []
  return (
    <>
      <SecRow
        left="Result"
        right={
          <span className="mono">
            {result.passed}/{result.total} passed · score {result.score ?? '—'}
          </span>
        }
      />
      {results.length === 0 ? (
        <Empty icon={FlaskConical}>
          {result.hasEvals === false ? 'No cases declared, so nothing ran.' : 'The run produced no results.'}
        </Empty>
      ) : (
        results.map((res) => (
          <div className="window" style={{ padding: '11px 15px', marginBottom: 10 }} key={res.id}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className="mono" style={{ fontWeight: 600 }}>
                {res.id}
              </span>
              {/* `statusClass` knows run statuses, not pass/fail, so the verdict
                  pill is spelled out here and the run's own status uses the shared
                  badge. */}
              <span className={`stbadge ${res.pass ? 'ok' : 'fail'}`}>
                <span className="d" />
                {res.pass ? 'pass' : 'fail'}
              </span>
              <span
                className="dim"
                style={{ fontSize: 11, marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}
              >
                {res.status ? <StatusBadge status={res.status} /> : null}
                {res.runId ? <CopyId id={res.runId} onCopied={onToast} /> : null}
              </span>
            </div>
            <div style={{ marginTop: 7 }}>
              {(res.checks ?? []).map((c) => (
                <div className="tm" key={c.check}>
                  {c.pass ? '✓' : '✗'} <span className="mono">{c.check}</span>
                  {c.detail ? ` — ${c.detail}` : ''}
                </div>
              ))}
              {/* An exception while running the case is not a failed check — the
                  case never got far enough to be scored. */}
              {res.error ? <div className="tm">✗ error — {res.error}</div> : null}
            </div>
          </div>
        ))
      )}
    </>
  )
}
