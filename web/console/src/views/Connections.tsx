import { CreditCard, FileText, Github, Globe, KeySquare, Slack } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { ConsoleState, Tool } from '../lib/types'
import { SecRow, Table, ViewHeader } from '../components/ui'

/**
 * A pure function of the `/console` aggregate — no fetch of its own. Connections and
 * the scoped-tool list both ride the shell's poll (`snapshot.py: build_console`), so
 * re-reading them here would just add a second source of truth for the same rows.
 *
 * The fields beyond `{id, provider, scopes}` are declared here rather than added to
 * `lib/types.ts`: they come straight off the store's connection record, so every one
 * of them is legitimately absent for a connection created before the field existed.
 */
interface Connection {
  id: string
  provider: string
  scopes?: string[]
  /** Absent means the credential is shared workspace-wide rather than per-user. */
  owner?: string | null
  status?: string | null
  secretSet?: boolean | null
  encrypted?: boolean | null
  label?: string | null
}

/** The `provider`/`scopes` half of a tool declaration — see `Tools.tsx` for the rest. */
interface ScopedTool extends Tool {
  scopes?: string[]
}

/**
 * Connection status → `.stbadge` tone, with **neutral for anything unrecognised**.
 *
 * The store's vocabulary is two words today and both backends do write it:
 * `create_connection`/`upsert_connection` set `"active"`, `revoke_connection` sets
 * `"revoked"` and destroys the secret (`store.py`, `store_postgres.py`). So the
 * unknown case is not an everyday one — but `_public_connection` passes through
 * whatever it finds rather than validating it, the broker proxies `list_connections`
 * for third-party duck-typed stores, and `Connection.status` above is optional for
 * the same reason every other field there is. A renderer that answers a value it has
 * never seen with a specific accusation is the thing being fixed; it must not be the
 * thing replacing it.
 *
 * Local rather than widened into `format.ts: STATUS_CLASS`, which is documented as
 * the RUN/job map. A domain with its own vocabulary keeps its own map — `Team.tsx`
 * does it for membership and `Quotas.tsx` for gate verdicts.
 */
const STATUS_TONE: Record<string, string> = { active: 'ok', revoked: 'fail' }

const PROVIDER_ICON: Record<string, LucideIcon> = {
  github: Github,
  slack: Slack,
  stripe: CreditCard,
  google: Globe,
  notion: FileText,
}

export function ConnectionsView({
  state,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const conns = (state.connections ?? []) as Connection[]
  // Only tools that name a provider participate in the intersection rule; the rest
  // need no credential at all, so listing them here would be noise.
  const scoped = (state.tools as ScopedTool[]).filter((t) => t.provider)

  return (
    <>
      <ViewHeader title="Connected credentials">
        Scoped credentials per provider — <strong>encrypted at rest</strong>, injected into tool
        calls at runtime, and enforced by the intersection rule (required ⊆ connection ∩ user). The
        secret is redacted from every trace and never returned to the agent.
      </ViewHeader>

      <Table
        rows={conns}
        // The connection id: a workspace can hold two connections to the same
        // provider (a shared one and a per-user one), so keying on `provider` would
        // collide and let a poll swap two rows' contents.
        rowKey={(c) => c.id}
        emptyIcon={KeySquare}
        emptyMessage="No connections — bind a credential with rya connect <provider>."
        columns={[
          {
            header: 'Provider',
            cell: (c) => {
              const Icon = PROVIDER_ICON[c.provider] ?? KeySquare
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
                  <span className="mono">{c.provider}</span>
                </>
              )
            },
          },
          { header: 'Scopes', cell: (c) => <Scopes scopes={c.scopes} /> },
          {
            header: 'Owner',
            // "shared" is a real state, not missing data: a connection with no owner
            // is bound at the workspace, and every user's calls resolve through it.
            cell: (c) =>
              c.owner ? <span className="mono dim">{c.owner}</span> : <span className="dim">shared</span>,
          },
          {
            header: 'Secret',
            cell: (c) =>
              !c.secretSet ? (
                <span className="dim">none</span>
              ) : c.encrypted ? (
                <span className="mono dim">
                  •••••••• <span style={{ color: 'var(--green)' }}>encrypted</span>
                </span>
              ) : (
                // Amber rather than red: an unencrypted secret at rest is a
                // configuration the operator can fix, not a failed connection.
                <span className="mono" style={{ color: 'var(--amber)' }}>
                  •••••••• unencrypted
                </span>
              ),
          },
          {
            header: 'Status',
            // Three states, like Secret directly above it. §5.17: this was a two-arm
            // ternary on `=== 'active'`, so BOTH of the other two came out as a red
            // "revoked" — a word the server never said. An absent status accused a
            // connection nobody had touched, whose secret is still set, of having been
            // revoked, and sent the operator off to re-issue a credential that is fine.
            //
            // Amber and not green, though, because a missing status is not cosmetic:
            // `get_connection` resolves a credential only `WHERE status = 'active'` in
            // both stores, so a connection whose status is absent or unrecognised will
            // NOT be injected into a tool call at runtime. Same reasoning as the amber
            // on `unencrypted` — something is wrong and an operator can fix it.
            //
            // "unknown" is a word this console is supplying, which is why it gets a
            // tone. An unrecognised status is a word the SERVER supplied, so it is
            // printed verbatim and left neutral: that is `StatusBadge`'s rule
            // (`components/ui.tsx` over `statusClass`, which returns '' for anything
            // it does not know), and this column had it exactly inverted.
            //
            // Both of those states carry a `title`, because the console's rule for an
            // unknown is that it says WHY it is unknown (§5.4's queue tiles, §5.10's
            // sidebar counts). A badge reading "unknown" with nothing behind it tells
            // an operator only that this page has stopped being useful; the runtime
            // consequence — the credential is not injected — is the part they need,
            // and it is the same consequence in both arms.
            cell: (c) =>
              !c.status ? (
                <span
                  className="stbadge wait"
                  title={
                    'This connection record carries no status. It will not be resolved into ' +
                    "tool calls, which match status = 'active'."
                  }
                >
                  <span className="d" />
                  unknown
                </span>
              ) : (
                <span
                  className={`stbadge ${STATUS_TONE[c.status] ?? ''}`.trim()}
                  title={
                    STATUS_TONE[c.status]
                      ? undefined
                      : `The runtime reported this connection as '${c.status}', which this console ` +
                        "does not recognise. Only status = 'active' is resolved into tool calls."
                  }
                >
                  <span className="d" />
                  {c.status}
                </span>
              ),
          },
        ]}
      />

      {/*
        Rendered only when some tool actually declares a provider. An agent with no
        scoped tools has nothing to say here, and an empty "Scoped tools" table would
        read as a missing binding rather than as a design without one.
      */}
      {scoped.length > 0 && (
        <>
          <SecRow left="Scoped tools" right="enforced at call time · required ⊆ connection ∩ user" />
          <Table
            rows={scoped}
            rowKey={(t) => t.id}
            columns={[
              { header: 'Tool', cell: (t) => <span className="mono">{t.id}</span> },
              { header: 'Provider', cell: (t) => <span className="mono">{t.provider}</span> },
              { header: 'Required scopes', cell: (t) => <Scopes scopes={t.scopes} /> },
            ]}
          />
        </>
      )}
    </>
  )
}

/** Scope chips, or an em dash for "no scope narrowing declared". */
function Scopes({ scopes }: { scopes?: string[] }) {
  if (!scopes?.length) return <span className="dim">—</span>
  return (
    <>
      {scopes.map((s, i) => (
        // Scopes are unique within a connection, so the scope string is a real key.
        // The separator sits outside the pill; inside it, it would widen the chip.
        <span key={s}>
          {i > 0 ? ' ' : null}
          <span className="ptag">{s}</span>
        </span>
      ))}
    </>
  )
}
