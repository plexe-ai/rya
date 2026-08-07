import {
  Activity,
  Cpu,
  Database,
  GitCommitVertical,
  Network,
  Server,
  ShieldCheck,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { fmtUptime } from '../lib/format'
import type { ConsoleState } from '../lib/types'
import { Empty, Window, ViewHeader } from '../components/ui'

// Ported from the legacy console's `renderInfra` (index.html:759-777). Pure state:
// the payload rides along on `GET /console` as `infra`, so this view needs no fetch
// of its own and no `useLoad` — it re-renders with the shell's poll like the views
// in `simple.tsx`.

/**
 * `GET /console` → `infra` (built by `api/app.py: _infrastructure`).
 *
 * `ConsoleState.infra` is typed `unknown` on purpose, so the narrowing happens
 * here rather than in `lib/types.ts`, and **every field is optional**. That is not
 * defensive padding: these are facts computed from the live process, and which of
 * them exist depends on the deployment mode. A single-tenant file-store dev server
 * has no `host`/`dbname`; a control plane serving published bundles has no loaded
 * manifest and therefore no `observability.traces`. A required field here would
 * compile and then throw on the deployment that omitted it.
 */
interface Infra {
  version?: string
  python?: string
  platform?: string
  pid?: number
  uptimeSeconds?: number
  environment?: string
  store?: { backend?: string; host?: string; dbname?: string; location?: string }
  auth?: { mode?: string; webhookSignature?: boolean; rls?: boolean }
  /**
   * `traces` is `null` whenever this process holds no manifest for the addressed
   * agent — which since D21 is every published bundle, because the api never
   * imports one (`_infrastructure`: `getattr(manifest, 'observability', None) and
   * …`). Exactly the three-state problem `agent.handlers` has: on, off, and *not
   * declared*. Rendering "off" for the third would claim tracing is disabled on a
   * deployment that simply never told us, so the third state gets its own words.
   */
  observability?: { traces?: boolean | null; export?: string }
  planes?: { controlPlane?: string; dataPlane?: string }
  endpoints?: string[]
  realtime?: { websocket?: string; protocol?: string }
}

/** One `k`/`v` line of a spec card. `tone` maps onto `.ispc .rw .v.ok` / `.v.dim`. */
interface SpecRow {
  k: string
  v: string
  tone?: 'ok' | 'dim'
}

/**
 * The legacy `spec()` helper as a component. It lives here rather than in
 * `components/ui.tsx` because Infrastructure is its only caller; the class names
 * (`ispc`/`hd`/`rw`/`k`/`v`) are the ones already in `styles.css`, so the card
 * looks identical to the one at `/`.
 */
function Spec({ icon: Icon, title, rows }: { icon: LucideIcon; title: string; rows: SpecRow[] }) {
  return (
    <div className="ispc">
      <div className="hd">
        <Icon aria-hidden="true" focusable="false" />
        <span className="t">{title}</span>
      </div>
      {rows.map((r) => (
        <div className="rw" key={r.k}>
          <span className="k">{r.k}</span>
          <span className={`v${r.tone ? ' ' + r.tone : ''}`}>{r.v}</span>
        </div>
      ))}
    </div>
  )
}

const DASH = '—'

export function InfrastructureView({
  state,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const infra = (state.infra ?? null) as Infra | null

  return (
    <>
      <ViewHeader title="Infrastructure">
        Live facts about the runtime serving this agent — compute, data, security,
        observability.
      </ViewHeader>
      {infra ? <Cards infra={infra} /> : <Empty icon={Server}>Infrastructure info unavailable.</Empty>}
    </>
  )
}

function Cards({ infra }: { infra: Infra }) {
  const st = infra.store ?? {}
  const auth = infra.auth ?? {}
  const obs = infra.observability ?? {}
  const planes = infra.planes ?? {}
  const rt = infra.realtime ?? {}

  // A Postgres store reports host + database; a file store reports a path. Neither
  // is guaranteed, so both fall through to a dash rather than "undefined/".
  const location = st.host ? `${st.host}/${st.dbname ?? ''}` : (st.location ?? DASH)
  const postgres = st.backend === 'postgres'
  const rls = !!auth.rls
  const traces = obs.traces

  return (
    <>
      <div className="ispec">
        <Spec
          icon={Cpu}
          title="Compute"
          rows={[
            { k: 'runtime', v: infra.python ? `Python ${infra.python}` : DASH },
            { k: 'platform', v: infra.platform ?? DASH },
            { k: 'process', v: infra.pid != null ? `pid ${infra.pid}` : DASH },
            { k: 'uptime', v: fmtUptime(infra.uptimeSeconds) },
            // The legacy card also printed a hard-coded `workers: 1 · in-process`.
            // Not ported: it is a claim, not a fact, and it is false on any
            // deployment where the execution plane is separate — which since D21 is
            // the normal one. Workers scale to zero and back (§6), so the honest
            // answer comes from `GET /workers` on the Workers view.
          ]}
        />
        <Spec
          icon={Database}
          title="Data substrate"
          rows={[
            { k: 'store', v: st.backend ?? DASH },
            { k: 'location', v: location },
            { k: 'row-level security', v: rls ? 'enforced' : DASH, tone: rls ? 'ok' : 'dim' },
            {
              k: 'durable',
              v: postgres ? 'yes' : 'file (dev)',
              tone: postgres ? 'ok' : 'dim',
            },
          ]}
        />
        <Spec
          icon={ShieldCheck}
          title="Auth & security"
          rows={[
            { k: 'mode', v: auth.mode ?? DASH },
            {
              k: 'webhook signing',
              v: auth.webhookSignature ? 'HMAC' : 'off',
              tone: auth.webhookSignature ? 'ok' : 'dim',
            },
            { k: 'secrets', v: 'process env / Secrets Manager' },
          ]}
        />
        <Spec
          icon={Activity}
          title="Observability"
          rows={[
            // Three states, not two — see the `traces` note on `Infra` above.
            traces == null
              ? { k: 'run traces', v: 'not declared', tone: 'dim' }
              : { k: 'run traces', v: traces ? 'on' : 'off', tone: traces ? 'ok' : 'dim' },
            {
              k: 'export',
              v: obs.export ?? DASH,
              // "local trace store" is the fallback, not an export target.
              tone: obs.export && obs.export !== 'local trace store' ? 'ok' : 'dim',
            },
          ]}
        />
        <Spec
          icon={GitCommitVertical}
          title="Deployment"
          rows={[
            { k: 'version', v: infra.version ? `rya ${infra.version}` : DASH },
            { k: 'environment', v: infra.environment ?? DASH },
            { k: 'tenancy', v: rls ? 'multi-tenant' : 'single-tenant' },
          ]}
        />
        <Spec
          icon={Network}
          title="Control / data plane"
          rows={[
            { k: 'control plane', v: planes.controlPlane ?? DASH },
            { k: 'data plane', v: planes.dataPlane ?? DASH },
            // `realtime` is in the payload but the legacy card dropped it; the WS
            // path and frame protocol are the two things an operator wiring a
            // client actually has to know.
            { k: 'realtime', v: rt.websocket ?? DASH },
            { k: 'protocol', v: rt.protocol ?? DASH },
          ]}
        />
      </div>

      {infra.endpoints?.length ? (
        <Window name="edge endpoints">{infra.endpoints.join('\n')}</Window>
      ) : null}
    </>
  )
}
