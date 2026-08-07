import { useEffect, useState } from 'react'
import { ListChecks, Plus, Save, Shield, ShieldHalf, Target, TriangleAlert, X } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { useLoad } from '../lib/usePoll'
import type { ConsoleState } from '../lib/types'
import { Empty, SecRow, Table, Tile, ViewHeader } from '../components/ui'

// Ported from the legacy console's `loadGuard` (index.html:778-782), `renderGuard`
// (783-802), the row helpers `guardGroup`/`guardRow`/`addRule`/`collectGuard`
// (803-821) and `saveGuard` (822-827).
//
// The Action Guard is the egress allowlist: every outbound request the agent makes
// is checked against the SSRF blocklist, then the static rules (deny beats allow),
// then the default policy — before it leaves the process. So this is the one ported
// view that WRITES, and the write is the whole design constraint:
//
//  1. It loads on ENTRY (`useLoad`), never from the shell's 6s poll. A poll landing
//     mid-edit and replacing the rows under the cursor is precisely the bug class
//     this migration exists to delete. The legacy console's answer elsewhere was to
//     sniff `document.activeElement` and skip the re-render; here the rows are React
//     state, so there is nothing to sniff and no interval to lose a race with.
//  2. Draft vs saved is explicit. The operator edits a local draft and presses Save;
//     nothing autosaves on a keystroke. An allowlist that took effect halfway
//     through being typed would, under `default: deny`, either open a hole or break
//     the agent's egress for as long as the sentence was unfinished.
//  3. Rows are keyed by a client-side id, not by their array position. Index keys in
//     an editable table make a row's inputs jump to a different row's value the
//     moment the list reorders or a rule above is removed.

/** One static rule. The matcher reads `pattern` — see the note on `toPolicy`. */
interface GuardRule {
  action?: string
  kind?: string
  pattern?: string
  methods?: string[]
  note?: string
}

/** The policy document, as stored and as `PUT` back. `guard.py: _normalize`. */
interface GuardPolicyDoc {
  /** Free prose, evaluated by the LLM judge when no static rule matches. */
  policy?: string
  rules?: GuardRule[]
  ssrf?: boolean
  default?: string
  fail?: string
}

/** `guard.py: run_tests` — the self-test that scores the policy. */
interface GuardTests {
  total?: number
  passed?: number
  attacksBlocked?: number
  attacksTotal?: number
  benignFalseBlocks?: number
  benignTotal?: number
  accuracy?: number
}

/**
 * `GET /agents/{agent}/guard`.
 *
 * `exists` is `gp.enforced`: false means no policy anywhere, which is "no egress
 * policy configured" — an ordinary state on a fresh install, not a failure. `error`
 * is the other thing entirely: a policy that is present but unreadable, which makes
 * the guard fail closed.
 */
interface GuardResponse {
  agent?: string
  policy?: GuardPolicyDoc
  tests?: GuardTests
  exists?: boolean
  version?: string
  source?: string
  error?: string | null
}

/** `PUT /agents/{agent}/guard` — re-runs the suite against what was just saved. */
interface GuardSaveResponse {
  ok?: boolean
  tests?: GuardTests
  version?: string
}

type Action = 'allow' | 'deny'

/**
 * A rule while it is being edited.
 *
 * `methods` is a raw comma-separated string rather than `string[]` because that is
 * what the operator is typing: splitting on every keystroke would delete the comma
 * they just pressed. It becomes an array in `toPolicy`, on save, exactly where the
 * legacy `collectGuard` did it.
 *
 * `key` is a client-side identity. Rules have no server id, and their pattern (the
 * obvious candidate) is the field being edited — keying by it would remount the
 * input on every character and drop focus after one letter.
 */
interface DraftRule {
  key: string
  action: Action
  kind: string
  pattern: string
  methods: string
  note: string
}

interface Draft {
  prose: string
  ssrf: boolean
  default: Action
  fail: 'open' | 'closed'
  rules: DraftRule[]
}

let seq = 0
const uid = () => `gr${++seq}`

const KINDS = ['glob', 'prefix', 'exact']

function toDraft(doc: GuardPolicyDoc | undefined): Draft {
  const d = doc ?? {}
  return {
    prose: d.policy ?? '',
    // The server's defaults (`_normalize`): SSRF blocklist on, default deny, fail
    // closed. A policy that omits them is not a policy with them off.
    ssrf: d.ssrf !== false,
    default: d.default === 'allow' ? 'allow' : 'deny',
    fail: d.fail === 'open' ? 'open' : 'closed',
    rules: (d.rules ?? []).map((r) => ({
      key: uid(),
      action: r.action === 'deny' ? 'deny' : 'allow',
      kind: KINDS.includes(r.kind ?? '') ? (r.kind as string) : 'glob',
      pattern: r.pattern ?? '',
      methods: (r.methods ?? []).join(', '),
      note: r.note ?? '',
    })),
  }
}

/**
 * Draft → the document that gets PUT.
 *
 * The field is **`pattern`**. `guard.py: _matcher` reads `rule.get("pattern", "")`,
 * so a rule written with `url:` instead compiles to `startswith("")` — it matches
 * every URL, and under `default: deny` that one misspelling turns an allowlist into
 * "allow everything" while still reading like a locked-down policy.
 * `examples/crizac/rya.guard.yaml` has exactly that bug. This editor can only emit
 * `pattern`, which is the point of having the shape in one place.
 *
 * Patternless rows are dropped rather than saved: a rule with an empty pattern is
 * the `startswith("")` hazard again, arrived at from the other direction.
 */
function toPolicy(draft: Draft): GuardPolicyDoc {
  return {
    ssrf: draft.ssrf,
    default: draft.default,
    fail: draft.fail,
    policy: draft.prose,
    rules: draft.rules
      .filter((r) => r.pattern.trim())
      .map((r) => ({
        action: r.action,
        kind: r.kind,
        pattern: r.pattern.trim(),
        methods: r.methods
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        note: r.note.trim(),
      })),
  }
}

/** Stable serialization used only to answer "is this draft dirty?". */
const fingerprint = (draft: Draft) => JSON.stringify(toPolicy(draft))

export function GuardView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const agent = state.agent.name
  const { data, error, loading, reload } = useLoad<GuardResponse>(
    () => api<GuardResponse>(ag(agent, '/guard')),
    [agent],
  )

  const [draft, setDraft] = useState<Draft | null>(null)
  /** Fingerprint of the last SAVED policy, so "dirty" is a fact and not a guess. */
  const [baseline, setBaseline] = useState('')
  const [tests, setTests] = useState<GuardTests | null>(null)
  const [saving, setSaving] = useState(false)

  // Seeded from the loaded policy, and ONLY from it. `data`'s identity changes when
  // a load or a post-save reload lands — never on a re-render — so an in-progress
  // draft survives every parent poll. This is the whole reason the fetch is a
  // `useLoad` and not a `usePoll`.
  useEffect(() => {
    if (!data) return
    const next = toDraft(data.policy)
    setDraft(next)
    setBaseline(fingerprint(next))
    setTests(data.tests ?? null)
  }, [data])

  const dirty = !!draft && fingerprint(draft) !== baseline

  async function save() {
    if (!draft) return
    setSaving(true)
    try {
      const policy = toPolicy(draft)
      // PUT, agent-prefixed. Writes always go to the addressed agent's key: the
      // unprefixed `/guard` resolves the reserved `_` alias, and once a workspace
      // serves two agents that is a 400 (`E_AGENT_AMBIGUOUS`) rather than a guess —
      // which is the safe failure for a write that could otherwise rewrite the
      // wrong agent's allowlist.
      const r = await api<GuardSaveResponse>(ag(agent, '/guard'), {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ policy }),
      })
      const t = r.tests ?? {}
      onToast(`Policy saved · ${t.passed ?? 0}/${t.total ?? 0} tests pass`)
      // Refresh after the write, like Approvals: the server re-scores the suite and
      // stamps a version, so the view must show what was stored, not what was sent.
      await reload()
    } catch (e) {
      onToast(`Save failed — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  function patch(key: string, field: keyof Omit<DraftRule, 'key' | 'action'>, value: string) {
    setDraft((d) =>
      d ? { ...d, rules: d.rules.map((r) => (r.key === key ? { ...r, [field]: value } : r)) } : d,
    )
  }

  function addRule(action: Action) {
    setDraft((d) =>
      d
        ? { ...d, rules: [...d.rules, { key: uid(), action, kind: 'glob', pattern: '', methods: '', note: '' }] }
        : d,
    )
  }

  function removeRule(key: string) {
    setDraft((d) => (d ? { ...d, rules: d.rules.filter((r) => r.key !== key) } : d))
  }

  if (error) {
    return (
      <>
        <Header />
        <Empty icon={ShieldHalf}>
          {error === 'unauthorized'
            ? 'Connect with an operator token to load the guard policy.'
            : `Guard unavailable — ${error}`}
        </Empty>
      </>
    )
  }

  if (!draft) {
    return (
      <>
        <Header />
        <Empty icon={ShieldHalf}>{loading ? 'Loading policy…' : 'No guard policy available.'}</Empty>
      </>
    )
  }

  const t = tests ?? {}
  const allow = draft.rules.filter((r) => r.action === 'allow')
  const deny = draft.rules.filter((r) => r.action === 'deny')

  return (
    <>
      <Header />

      <div className="stats" style={{ marginBottom: 20 }}>
        <Tile
          icon={ListChecks}
          label="Policy test suite"
          value={`${t.passed ?? 0}/${t.total ?? 0}`}
          sub="cases"
        />
        <Tile
          icon={Shield}
          label="Attacks blocked"
          value={`${t.attacksBlocked ?? 0}/${t.attacksTotal ?? 0}`}
          sub="malicious requests"
        />
        <Tile
          icon={TriangleAlert}
          label="Benign false-blocks"
          value={`${t.benignFalseBlocks ?? 0}/${t.benignTotal ?? 0}`}
          sub="should pass"
          amber={(t.benignFalseBlocks ?? 0) > 0}
        />
        <Tile icon={Target} label="Decision accuracy" value={`${t.accuracy ?? 0}%`} sub="on the test suite" />
      </div>

      {/* No policy anywhere is an ordinary state, not an outage: nothing is being
          enforced yet, and the editor below is how one gets created. */}
      {data?.exists === false && (
        <Empty icon={ShieldHalf}>
          No egress policy configured — nothing is enforced yet. Add rules below and save to
          start one.
        </Empty>
      )}
      {/* Present but unreadable is the opposite case, and it is loud: the guard
          fails closed, so every outbound request is being blocked right now. */}
      {data?.error && (
        <Empty icon={TriangleAlert}>Policy unreadable, failing closed — {data.error}</Empty>
      )}

      <div className="gcard">
        <div className="gch">
          <span className="gct">Security policy</span>
          <span className="dim" style={{ fontSize: 12 }}>
            evaluated by the LLM judge when no static rule matches
          </span>
        </div>
        <textarea
          className="gtext"
          aria-label="Security policy"
          value={draft.prose}
          onChange={(e) => setDraft({ ...draft, prose: e.target.value })}
        />
        <div className="grow2">
          <span>when the judge errors or times out</span>
          <select
            className="gsel"
            aria-label="Judge failure mode"
            value={draft.fail}
            onChange={(e) => setDraft({ ...draft, fail: e.target.value === 'open' ? 'open' : 'closed' })}
          >
            <option value="closed">fail closed (block)</option>
            <option value="open">fail open (allow)</option>
          </select>
          <label style={{ marginLeft: 12 }}>
            <input
              type="checkbox"
              checked={draft.ssrf}
              onChange={(e) => setDraft({ ...draft, ssrf: e.target.checked })}
            />{' '}
            SSRF blocklist
          </label>
          <span style={{ marginLeft: 12 }}>default</span>
          <select
            className="gsel"
            aria-label="Default policy"
            value={draft.default}
            onChange={(e) => setDraft({ ...draft, default: e.target.value === 'allow' ? 'allow' : 'deny' })}
          >
            <option value="deny">deny</option>
            <option value="allow">allow</option>
          </select>
        </div>
      </div>

      <SecRow
        left="Static rules · checked first · deny beats allow"
        right={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10 }}>
            {/* The draft state, in words. Without this, "did my edit take?" has no
                answer short of reloading the page. */}
            <span className="dim">{dirty ? 'unsaved changes' : 'saved'}</span>
            <button className="btn dark sm" onClick={() => void save()} disabled={saving || !dirty}>
              <Save aria-hidden="true" focusable="false" />
              {saving ? 'Saving…' : 'Save policy'}
            </button>
          </span>
        }
      />

      <RuleGroup
        action="allow"
        rules={allow}
        onAdd={() => addRule('allow')}
        onPatch={patch}
        onRemove={removeRule}
      />
      <RuleGroup
        action="deny"
        rules={deny}
        onAdd={() => addRule('deny')}
        onPatch={patch}
        onRemove={removeRule}
      />
    </>
  )
}

function Header() {
  return (
    <ViewHeader title="Action Guard">
      Every outbound request the agent makes is checked against the SSRF blocklist and the rules
      below, then forwarded or blocked — before it leaves the process. Edits take effect when you
      save.
    </ViewHeader>
  )
}

/**
 * One action group as an editable table.
 *
 * `rowKey` is the rule's client id: with an index key, removing the first allow rule
 * would leave every input below it showing the previous row's value, because React
 * would match the old inputs to the new positions.
 */
function RuleGroup({
  action,
  rules,
  onAdd,
  onPatch,
  onRemove,
}: {
  action: Action
  rules: DraftRule[]
  onAdd: () => void
  onPatch: (key: string, field: keyof Omit<DraftRule, 'key' | 'action'>, value: string) => void
  onRemove: (key: string) => void
}) {
  const n = (r: DraftRule) => `${action} rule ${rules.indexOf(r) + 1}`
  return (
    <>
      <div className="gghd">
        <span className={`gtag ${action}`}>{action.toUpperCase()}</span>
        <span className="dim">{rules.length}</span>
        <button className="btn sm" style={{ marginLeft: 'auto' }} onClick={onAdd}>
          <Plus aria-hidden="true" focusable="false" />
          add {action}
        </button>
      </div>
      <Table
        rows={rules}
        rowKey={(r) => r.key}
        emptyIcon={ShieldHalf}
        emptyMessage={
          action === 'allow'
            ? 'No allow rules — under a default of deny, nothing is allowed out.'
            : 'No deny rules.'
        }
        columns={[
          {
            header: 'Kind',
            cell: (r) => (
              <select
                className="gsel"
                aria-label={`${n(r)} kind`}
                value={r.kind}
                onChange={(e) => onPatch(r.key, 'kind', e.target.value)}
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            ),
          },
          {
            header: 'Pattern',
            cell: (r) => (
              <input
                className="gin pat mono"
                style={{ width: '100%' }}
                aria-label={`${n(r)} pattern`}
                placeholder="https://api.example.com/*"
                value={r.pattern}
                onChange={(e) => onPatch(r.key, 'pattern', e.target.value)}
              />
            ),
          },
          {
            header: 'Methods',
            cell: (r) => (
              <input
                className="gin meth mono"
                aria-label={`${n(r)} methods`}
                placeholder="any method"
                value={r.methods}
                onChange={(e) => onPatch(r.key, 'methods', e.target.value)}
              />
            ),
          },
          {
            header: 'Note',
            cell: (r) => (
              <input
                className="gin note"
                style={{ width: '100%' }}
                aria-label={`${n(r)} note`}
                placeholder="note"
                value={r.note}
                onChange={(e) => onPatch(r.key, 'note', e.target.value)}
              />
            ),
          },
          {
            header: '',
            cell: (r) => (
              <button className="grm" aria-label={`Remove ${n(r)}`} title="Remove rule" onClick={() => onRemove(r.key)}>
                <X aria-hidden="true" focusable="false" />
              </button>
            ),
          },
        ]}
      />
    </>
  )
}
