import { useState } from 'react'
import { Plug, Power, PowerOff } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { PERM_CLASS } from '../lib/format'
import type { ConsoleState, Permission, Tool } from '../lib/types'
import { SecRow, Table, ViewHeader } from '../components/ui'

/**
 * The extra per-tool fields `GET /console` carries that `lib/types.ts: Tool` does
 * not model yet (`snapshot.py: build_console`). Declared here rather than added to
 * the shared type so this port touches no existing file; all of them are genuinely
 * optional, since `externalSideEffects` and `requiredSecrets` are read off the tool
 * REGISTRY and a published bundle the control plane never imported has no registry
 * entry to read them from.
 */
interface SnapshotTool extends Tool {
  calls?: number
  externalSideEffects?: boolean | null
  requiredSecrets?: string[]
  /** True only for a registry mock: a project `@agent.tool` or a `url:` decl is real IO. */
  mockImpl?: boolean
  scopes?: string[]
}

/** One entry of `GET /agents/{agent}/tools` — `api/app.py: _tools_of`. */
interface ToolSwitch {
  id: string
  /** What the manifest declares. */
  permission: Permission
  /** Manifest permission unless a runtime kill switch overrides it. */
  effectivePermission?: Permission
  /**
   * The override itself, or null/absent when the manifest is in force. Carries
   * `permission`, `ts` and `reason`; `version` belongs to the policy RECORD, so it
   * is present on the PUT response but not on every read — hence optional.
   */
  override?: { permission: Permission; ts?: string; reason?: string | null; version?: number } | null
}

interface ToolsResponse {
  agent?: string
  tools?: ToolSwitch[]
}

/** `PUT /agents/{agent}/tools/{id}/permission` — `api/app.py: _set_tool_permission`. */
interface SwitchResult {
  tool: string
  permission: Permission
  previous?: Permission
  cleared?: boolean
  /** Version of the privileged policy record the write produced (append-only, attributed). */
  version?: number
  actor?: string | null
  ts?: string
}

/**
 * What each tier MEANS at call time, because a permission pill that only names a
 * tier reads like a label on a shelf.
 *
 * The runtime enforces this, not the prompt: a `disabled` tool is still declared and
 * still visible to the model, and the refusal happens when it is called. Likewise
 * `approval_required` does not filter the tool out, it suspends the run mid-flight.
 * Saying so is the difference between an operator trusting the switch and an
 * operator assuming the model was quietly told to behave.
 */
const PERM_MEANING: Record<string, string> = {
  allowed: 'Callable. No gate at call time.',
  read_only: 'Callable, but declared to have no external side effects.',
  approval_required: 'The run PAUSES at this call and waits for a human decision.',
  disabled: 'The call is REFUSED by the runtime. The tool is not hidden from the model.',
}

function PermPill({ permission }: { permission: Permission }) {
  return (
    <span className={`pb ${PERM_CLASS[permission] ?? ''}`} title={PERM_MEANING[permission]}>
      {permission}
    </span>
  )
}

/**
 * Tool registry + the runtime kill switches.
 *
 * The kill-switch column comes from a SECOND request. The tool list itself is in the
 * `/console` aggregate the shell polls every 6s, but effective permissions and the
 * active overrides live behind `GET /agents/{agent}/tools`, so the legacy console had
 * to funnel both through one `renderTools` and cache the enriched half in a module
 * global (`TOOL_EFF`) purely so the poll could not blow the column away mid-click.
 *
 * Here the poll arrives as new props and the switch data is local state, so the two
 * cannot overwrite each other. What remains is a KEYING question: `rowKey` is the tool
 * id, so a poll that reorders or re-fetches the list re-uses the same `<tr>` and the
 * button an operator is reaching for does not move or lose its pending state. An index
 * key would silently reintroduce exactly the hazard `TOOL_EFF` was working around.
 */
export function ToolsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const tools = state.tools as SnapshotTool[]

  // Loads on entry, not from the 6s poll: an override is written by an operator
  // pressing a button, so it moves on a write and not per second. Keyed on the agent
  // name so switching agents re-reads rather than showing one agent's overrides
  // under another's tools.
  const { data, error, reload } = useLoad(
    () => api<ToolsResponse>(ag(state.agent.name, '/tools')),
    [state.agent.name],
  )

  // Per-tool, not one flag: disabling `email.send` must not freeze every other row.
  const [pending, setPending] = useState<Record<string, boolean>>({})

  const eff: Record<string, ToolSwitch> = {}
  for (const t of data?.tools ?? []) eff[t.id] = t

  // Deliberately `data !== null` and NOT `!loading`: `reload()` flips loading back
  // to true, and keying the column set off that would collapse the kill-switch
  // column for the duration of every refresh-after-write. Same rule the legacy
  // renderer encoded by caching `TOOL_EFF` across renders.
  const enriched = data !== null

  /**
   * Flip a runtime kill switch. `mode: 'disabled'` writes the override,
   * `mode: 'clear'` drops it so the manifest permission takes over again.
   *
   * PUT, and agent-prefixed. The agent is addressed so the server can check the tool
   * against a real declaration (404 `E_TOOL_NOT_FOUND` otherwise) — the switch it
   * writes is workspace-wide privileged policy state, versioned and attributed, which
   * is also why the bundle whose tool is being killed cannot write it back.
   */
  async function toolSwitch(id: string, mode: 'disabled' | 'clear') {
    setPending((p) => ({ ...p, [id]: true }))
    const payload = mode === 'clear' ? { clear: true } : { permission: mode, reason: 'console kill switch' }
    try {
      const r = await api<SwitchResult>(
        ag(state.agent.name, `/tools/${encodeURIComponent(id)}/permission`),
        {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        },
      )
      const verb = mode === 'clear' ? 'Restored' : 'Disabled'
      const version = r.version != null ? ` · v${r.version}` : ''
      onToast(`${verb} ${id}${version} — effective immediately`)
      // Refresh-after-write: the response says what the switch became, but the table
      // renders the server's view of every tool, so re-read rather than patch.
      await reload()
    } catch (e) {
      onToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      // Both paths: on success the row re-renders from fresh data, on failure the
      // operator needs the button back.
      setPending((p) => {
        const { [id]: _drop, ...rest } = p
        return rest
      })
    }
  }

  return (
    <>
      <ViewHeader title="Tool registry">
        Every tool the agent can reach — typed, permissioned, audited. Credentials resolve in the
        runtime; the agent never sees them.
      </ViewHeader>

      <SecRow
        left="Tools"
        right={
          enriched
            ? `${tools.length} declared · permission enforced at call time, not by the prompt`
            : `${tools.length} declared · manifest permissions`
        }
      />

      {enriched ? (
        <Table
          rows={tools}
          // The tool id. See the note above: this is what lets a 6s poll land
          // without disturbing a row an operator is mid-interaction with.
          rowKey={(t) => t.id}
          emptyIcon={Plug}
          emptyMessage="No tools declared — add one to rya.agent.yaml or register @agent.tool."
          columns={[
            { header: 'Tool', cell: (t) => <ToolName tool={t} /> },
            { header: 'Manifest', cell: (t) => <PermPill permission={t.permission} /> },
            {
              header: 'Effective',
              cell: (t) => {
                const e = eff[t.id]
                const ep = e?.effectivePermission ?? t.permission
                const ov = e?.override
                return (
                  <>
                    <PermPill permission={ep} />
                    {ov && (
                      <>
                        {' '}
                        <span
                          className="ptag"
                          title={[
                            'Runtime override active — the manifest says ' + t.permission + '.',
                            ov.reason ? `Reason: ${ov.reason}` : null,
                            ov.ts ? `Set ${ov.ts}` : null,
                          ]
                            .filter(Boolean)
                            .join(' ')}
                        >
                          {ov.version != null ? `override v${ov.version}` : 'override'}
                        </span>
                      </>
                    )}
                  </>
                )
              },
            },
            {
              header: 'Side effects',
              cell: (t) => (t.externalSideEffects ? 'external' : <span className="dim">none</span>),
            },
            { header: 'Calls', cell: (t) => String(t.calls ?? 0) },
            {
              header: 'Kill switch',
              cell: (t) => {
                const ep = eff[t.id]?.effectivePermission ?? t.permission
                const busy = !!pending[t.id]
                return ep !== 'disabled' ? (
                  <button
                    className="btn sm"
                    onClick={() => void toolSwitch(t.id, 'disabled')}
                    disabled={busy}
                    title="Refuse every call to this tool from now on, without a redeploy."
                  >
                    <PowerOff aria-hidden="true" focusable="false" />
                    Disable
                  </button>
                ) : (
                  <button
                    className="btn sm"
                    onClick={() => void toolSwitch(t.id, 'clear')}
                    disabled={busy}
                    title="Drop the override and fall back to the manifest permission."
                  >
                    <Power aria-hidden="true" focusable="false" />
                    Restore
                  </button>
                )
              },
            },
          ]}
        />
      ) : (
        // Before the switch read lands — and if it fails outright — the snapshot's
        // own columns are still true and still useful, exactly as the legacy
        // console's empty catch left the first-pass table in place. The one thing
        // not to do is imply there are no kill switches.
        <Table
          rows={tools}
          rowKey={(t) => t.id}
          emptyIcon={Plug}
          emptyMessage="No tools declared — add one to rya.agent.yaml or register @agent.tool."
          columns={[
            { header: 'Tool', cell: (t) => <ToolName tool={t} /> },
            { header: 'Permission', cell: (t) => <PermPill permission={t.permission} /> },
            {
              header: 'Side effects',
              cell: (t) => (t.externalSideEffects ? 'external' : <span className="dim">none</span>),
            },
            {
              header: 'Secret',
              cell: (t) =>
                t.requiredSecrets?.length ? (
                  <span className="mono dim">{t.requiredSecrets.join(', ')}</span>
                ) : (
                  <span className="dim">—</span>
                ),
            },
            { header: 'Calls', cell: (t) => String(t.calls ?? 0) },
          ]}
        />
      )}

      {error && (
        <div className="sub" style={{ marginTop: 12 }}>
          Effective permissions and kill switches are unavailable right now ({error}). The
          permissions above are what the manifest declares; a runtime override, if one is
          active, would not show here.
        </div>
      )}

      <SecRow left="What a tier does" right="enforced by the runtime, not the prompt" />
      <Table
        rows={['allowed', 'read_only', 'approval_required', 'disabled'] as Permission[]}
        rowKey={(p) => p}
        columns={[
          { header: 'Tier', cell: (p) => <PermPill permission={p} /> },
          { header: 'At call time', cell: (p) => PERM_MEANING[p] },
        ]}
      />
    </>
  )
}

/** Tool id, plus the `mock` marker for a tool whose implementation is demo data. */
function ToolName({ tool }: { tool: SnapshotTool }) {
  return (
    <>
      <span className="mono">{tool.id}</span>
      {tool.mockImpl && (
        <>
          {' '}
          <span
            className="ptag"
            style={{ color: 'var(--amber)' }}
            title="Deterministic mock implementation — demo data, not real IO."
          >
            mock
          </span>
        </>
      )}
    </>
  )
}
