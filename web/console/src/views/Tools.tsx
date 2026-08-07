import { useId, useState } from 'react'
import { Plug, Power, PowerOff } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import { PERM_CLASS } from '../lib/format'
import type { ConsoleState, Permission, Tool } from '../lib/types'
import { SecRow, Table, ViewHeader } from '../components/ui'
import { ConfirmDialog } from '../components/ConfirmDialog'

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

/**
 * Every tier the server will accept, in escalating order of restriction.
 *
 * One list, read by both the legend at the foot of this view and the picker in the
 * override dialog, because §5.15 was precisely the gap between them: the legend
 * documented four tiers and the column offered a hardcoded `disabled` and a clear, so
 * "put this tool behind approval until we've looked at it" — the tier that suspends a
 * run instead of failing it, and the one an operator actually wants during an incident
 * — was explained at length and then unreachable. Reading them off the same constant
 * is what keeps a tier from being documented and unoffered again.
 */
const PERM_TIERS: Permission[] = ['allowed', 'read_only', 'approval_required', 'disabled']

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

  // Which row's switch is being confirmed, if any. Null is the normal state, so no
  // path through this view writes policy without passing through the dialog below.
  const [confirming, setConfirming] = useState<{ id: string; mode: 'set' | 'clear' } | null>(null)

  const eff: Record<string, ToolSwitch> = {}
  for (const t of data?.tools ?? []) eff[t.id] = t

  // Deliberately `data !== null` and NOT `!loading`: `reload()` flips loading back
  // to true, and keying the column set off that would collapse the kill-switch
  // column for the duration of every refresh-after-write. Same rule the legacy
  // renderer encoded by caching `TOOL_EFF` across renders.
  const enriched = data !== null

  const confirmingTool = confirming ? tools.find((t) => t.id === confirming.id) : undefined

  /**
   * Flip a runtime kill switch, once an operator has confirmed the decision.
   *
   * PUT, and agent-prefixed. The agent is addressed so the server can check the tool
   * against a real declaration (404 `E_TOOL_NOT_FOUND` otherwise) — the switch it
   * writes is workspace-wide privileged policy state, versioned and attributed, which
   * is also why the bundle whose tool is being killed cannot write it back.
   *
   * The `reason` comes from the operator and nowhere else. It used to be the constant
   * `'console kill switch'` (§5.15), which is worth being precise about: the server
   * stores it verbatim in an append-only policy record beside a real actor and a real
   * timestamp, and `GET /tools/log` exists to answer "who changed which kill switch,
   * when, and what it was before". A hardcoded string does not merely add nothing to
   * that record — it fills the only field capable of answering *why* with a sentence
   * describing the button that was pressed, permanently, under someone's name. Six
   * months later the log says the same thing for the tool killed during an outage and
   * the one killed by a mis-click.
   *
   * Returns whether the write landed, so the dialog can stay open on failure rather
   * than discarding a reason the operator has just typed.
   */
  async function toolSwitch(id: string, decision: Decision): Promise<boolean> {
    setPending((p) => ({ ...p, [id]: true }))
    // `reason` is deliberately absent from the clear payload, not empty: the server
    // ignores it on a clear (it removes the record rather than annotating one), and
    // sending a field that is dropped on the floor invites the next reader to think
    // the log will carry it.
    const payload =
      decision.mode === 'clear'
        ? { clear: true }
        : { permission: decision.permission, reason: decision.reason }
    try {
      const r = await api<SwitchResult>(
        ag(state.agent.name, `/tools/${encodeURIComponent(id)}/permission`),
        {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        },
      )
      const what =
        decision.mode === 'clear'
          ? `Restored ${id}`
          : decision.permission === 'disabled'
            ? `Disabled ${id}`
            : `${id} → ${decision.permission}`
      const version = r.version != null ? ` · v${r.version}` : ''
      onToast(`${what}${version} — effective immediately`)
      // Refresh-after-write: the response says what the switch became, but the table
      // renders the server's view of every tool, so re-read rather than patch.
      await reload()
      return true
    } catch (e) {
      onToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
      return false
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
                // Both labels end in an ellipsis on purpose: it is the conventional
                // signal that the control opens a dialog rather than acting, and this
                // column sits in a table a 6s poll keeps repainting, where the cost of
                // a mis-click used to be a live tool refusing every call (§5.15).
                return ep !== 'disabled' ? (
                  <button
                    className="btn sm"
                    onClick={() => setConfirming({ id: t.id, mode: 'set' })}
                    disabled={busy}
                    title="Override this tool's permission at runtime, without a redeploy."
                  >
                    <PowerOff aria-hidden="true" focusable="false" />
                    Disable…
                  </button>
                ) : (
                  <button
                    className="btn sm"
                    onClick={() => setConfirming({ id: t.id, mode: 'clear' })}
                    disabled={busy}
                    title="Drop the override and fall back to the manifest permission."
                  >
                    <Power aria-hidden="true" focusable="false" />
                    Restore…
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
        rows={PERM_TIERS}
        rowKey={(p) => p}
        columns={[
          { header: 'Tier', cell: (p) => <PermPill permission={p} /> },
          { header: 'At call time', cell: (p) => PERM_MEANING[p] },
        ]}
      />

      {/* Mounted only while a decision is open, and keyed on the row it belongs to, so
          the tier and the reason inside it start empty for every tool. A reason typed
          for `email.send` that survived into the dialog for `billing.refund` would be
          worse than no reason at all: it would be a plausible, wrong sentence in an
          append-only audit record. If a poll retires the tool underneath an open
          dialog it unmounts, which is the honest outcome — there is no longer anything
          to override. */}
      {confirmingTool && confirming && (
        <ToolPermissionDialog
          key={`${confirming.id}:${confirming.mode}`}
          toolId={confirming.id}
          mode={confirming.mode}
          manifest={confirmingTool.permission}
          effective={eff[confirming.id]?.effectivePermission ?? confirmingTool.permission}
          busy={!!pending[confirming.id]}
          onCancel={() => setConfirming(null)}
          onConfirm={async (decision) => {
            if (await toolSwitch(confirming.id, decision)) setConfirming(null)
          }}
        />
      )}
    </>
  )
}

/** What a confirmed decision carries to the wire. */
type Decision = { mode: 'set'; permission: Permission; reason: string } | { mode: 'clear' }

/**
 * The kill switch's confirmation step.
 *
 * A `ConfirmDialog` plus the two fields this particular decision needs, rather than a
 * second general dialog: the tier picker and the reason box are specific to writing a
 * tool permission, and the shared component stays a shared component by not learning
 * about them. It owns its own field state, so mounting it is what clears the form.
 *
 * The two modes are asymmetric on purpose. Writing an override demands a reason,
 * because that string is the entire content of the audit record — a required field is
 * the only reason `GET /tools/log` will be worth reading in six months. Clearing one
 * does not: the server drops the record rather than annotating it, so a reason typed
 * here would go nowhere. **It is still confirmed**, because restoring is not a neutral
 * undo — it re-enables a tool that somebody deliberately killed, quite possibly during
 * an incident that is still running, and "I clicked the wrong row of a table that
 * repaints every six seconds" should not be able to turn refunds back on.
 */
function ToolPermissionDialog({
  toolId,
  mode,
  manifest,
  effective,
  busy,
  onCancel,
  onConfirm,
}: {
  toolId: string
  mode: 'set' | 'clear'
  manifest: Permission
  effective: Permission
  busy: boolean
  onCancel: () => void
  onConfirm: (decision: Decision) => void
}) {
  // `disabled` is the default because this column is the kill switch and killing the
  // tool is what an operator reaching for it usually means. The other three are one
  // keystroke away rather than unreachable, which is the whole of §5.15.
  const [permission, setPermission] = useState<Permission>('disabled')
  const [reason, setReason] = useState('')
  const uid = useId()

  if (mode === 'clear') {
    return (
      <ConfirmDialog
        title={`Restore ${toolId}?`}
        body={`Drops the runtime override and falls back to what the manifest declares: ${manifest}. The tool becomes callable again immediately, on every run in this workspace.`}
        confirmLabel="Drop override"
        busy={busy}
        onCancel={onCancel}
        onConfirm={() => onConfirm({ mode: 'clear' })}
      />
    )
  }

  const trimmed = reason.trim()
  return (
    <ConfirmDialog
      title={`Override ${toolId}?`}
      body={`Manifest says ${manifest}; in force right now is ${effective}. This writes a versioned, attributed policy record that takes effect on the next call — no redeploy, and no way to un-write the record.`}
      confirmLabel="Write override"
      // Red only when the choice actually refuses calls. Moving a tool to
      // approval_required is consequential, not destructive, and colouring the two the
      // same would make the warning mean nothing.
      danger={permission === 'disabled'}
      busy={busy}
      // The gate on the reason lives here rather than in a submit handler so the
      // operator can see that the field is what is holding the action back.
      confirmDisabled={!trimmed}
      onCancel={onCancel}
      onConfirm={() => onConfirm({ mode: 'set', permission, reason: trimmed })}
    >
      <label className="fl" htmlFor={`${uid}-perm`}>
        New permission
      </label>
      <select
        id={`${uid}-perm`}
        value={permission}
        onChange={(e) => setPermission(e.currentTarget.value as Permission)}
      >
        {PERM_TIERS.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
      {/* The same sentence the legend gives that tier, next to the control that
          selects it — a picker whose options are four snake_case identifiers asks the
          operator to already know what they mean. */}
      <div className="dim" style={{ fontSize: 12, margin: '-7px 0 13px' }}>
        {PERM_MEANING[permission]}
      </div>

      <label className="fl" htmlFor={`${uid}-reason`}>
        Reason — recorded against your identity in the tool policy log
      </label>
      <textarea
        id={`${uid}-reason`}
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
        placeholder="e.g. vendor incident INC-4412 — refunds paused until the postmortem"
      />
    </ConfirmDialog>
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
