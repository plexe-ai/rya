import {
  Activity, BookOpenText, Box, Clock, Coins, Database, Fingerprint, GitBranch, KeyRound,
  KeySquare, Layers, ListOrdered, MessagesSquare, Play, Plug, PlugZap, RefreshCw, Send,
  UserCheck, Zap,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { num, usd } from '../lib/format'
import type { ViewId } from '../lib/nav'
import type { ConsoleState } from '../lib/types'
import { SecRow, Tile, Window } from '../components/ui'

function PrimitiveCard({
  icon: Icon,
  name,
  line,
  tags,
  amber,
  onClick,
}: {
  icon: LucideIcon
  name: string
  line: string
  tags: (string | false | null | undefined)[]
  amber?: boolean
  onClick: () => void
}) {
  return (
    <button className="pcard" onClick={onClick}>
      <div className="ph">
        <span className="pic">
          <Icon aria-hidden="true" focusable="false" />
        </span>
        <h3>{name}</h3>
        <span className={`pstat${amber ? ' amber' : ''}`} />
      </div>
      <div className="pline">{line}</div>
      <div className="ptags">
        {tags.filter(Boolean).map((t, i) => (
          <span className="ptag" key={i}>
            {t}
          </span>
        ))}
      </div>
    </button>
  )
}

/** Shown in place of the stat tiles when the runtime is unreachable AND we have no data. */
export function RuntimeDown({
  message,
  onRetry,
  onEnterToken,
}: {
  message: string
  onRetry: () => void
  onEnterToken: () => void
}) {
  return (
    <div className="downcard">
      <div className="dic">
        <PlugZap aria-hidden="true" focusable="false" />
      </div>
      <h3>Can&apos;t reach the runtime</h3>
      <p>
        {message}. Make sure <span className="mono">rya serve</span> is running.
      </p>
      <div className="dbtns">
        <button className="btn dark sm" onClick={onRetry}>
          <RefreshCw aria-hidden="true" focusable="false" />
          Retry
        </button>
        <button className="btn sm" onClick={onEnterToken}>
          <KeyRound aria-hidden="true" focusable="false" />
          Enter token
        </button>
      </div>
    </div>
  )
}

export function OverviewView({
  state,
  onNavigate,
  deployCounts,
}: {
  state: ConsoleState
  onNavigate: (v: ViewId) => void
  deployCounts?: { envs: number; versions: number }
}) {
  const st = state.stats
  const gated = state.tools.filter((t) => t.permission === 'approval_required').length
  const docs = state.knowledge?.documents.length ?? 0
  const chunks = state.knowledge?.chunks ?? 0
  const connections = state.connections ?? []
  const cronCount = state.triggers.filter((t) => t.type === 'cron').length

  // `rya context --json` is reproduced verbatim so the terminal panel matches what
  // the CLI prints for the same agent.
  const contextJson = [
    '$ rya context --json',
    '{',
    `  "agent": "${state.agent.name}",`,
    `  "runtime": { "store": "${state.runtime.store}", "llmProvider": "${state.runtime.llmProvider}", "multiTenant": ${state.runtime.multiTenant} },`,
    `  "tools": [${state.tools
      .map((t) => `"${t.id}${t.permission === 'approval_required' ? ':approval_required' : ''}"`)
      .join(', ')}],`,
    `  "approvals": { "pending": ${st.approvalsPending} },`,
    `  "runs": { "total": ${st.runs}${Object.entries(st.byStatus)
      .map(([k, v]) => `, "${k}": ${v}`)
      .join('')} }`,
    '}',
  ].join('\n')

  return (
    <>
      <div className="stats">
        <Tile icon={Play} label="Runs" value={num(st.runs)} sub={`${st.byStatus.completed || 0} completed`} />
        <Tile
          icon={UserCheck}
          label="Approvals"
          value={num(st.approvalsPending)}
          sub="pending review"
          amber={st.approvalsPending > 0}
        />
        <Tile
          icon={Coins}
          label="Tokens"
          value={num(st.inputTokens + st.outputTokens)}
          sub={usd(st.costUsd) ?? 'across all runs'}
        />
        <Tile icon={Clock} label="Jobs queued" value={num(st.jobsPending)} sub="background" />
      </div>

      <SecRow left="Primitives" right="from the manifest" />

      <div className="pgrid">
        <PrimitiveCard
          icon={Fingerprint}
          name="Identity"
          line="Bound to every run."
          tags={[state.runtime.multiTenant ? 'per-user RLS' : 'single-tenant', `v${state.agent.version}`]}
          onClick={() => onNavigate('runs')}
        />
        <PrimitiveCard
          icon={Box}
          name="Runtime"
          line="Pauses, resumes, retries."
          tags={[state.agent.handlers.event ? 'handler ✓' : 'no handler', state.runtime.store]}
          onClick={() => onNavigate('runs')}
        />
        <PrimitiveCard
          icon={GitBranch}
          name="Deployments"
          line="Content-hashed versions; an environment points at one."
          tags={
            deployCounts
              ? [`${deployCounts.envs} environments`, `${deployCounts.versions} versions`]
              : ['environment → version → runs']
          }
          onClick={() => onNavigate('deploy')}
        />
        <PrimitiveCard
          icon={Database}
          name="Memory"
          line="Blocks + long-term facts."
          tags={[
            `${(state.memory.blocks ?? []).length} blocks`,
            `${state.memory.facts ?? 0} facts`,
            `+${state.memory.collections.length} collections`,
          ]}
          onClick={() => onNavigate('memory')}
        />
        <PrimitiveCard
          icon={BookOpenText}
          name="Knowledge"
          line="RAG over documents."
          tags={[`${docs} docs`, `${chunks} chunks`]}
          onClick={() => onNavigate('knowledge')}
        />
        <PrimitiveCard
          icon={Plug}
          name="Tools"
          line="Typed, permissioned, audited."
          tags={[`${state.tools.length} registered`, `${gated} gated`]}
          onClick={() => onNavigate('tools')}
        />
        <PrimitiveCard
          icon={Layers}
          name="Model gateway"
          line="Permissioned, versioned, traced."
          tags={[state.runtime.llmProvider, ...state.models.slice(0, 2).map((m) => m.id)]}
          onClick={() => onNavigate('models')}
        />
        <PrimitiveCard
          icon={MessagesSquare}
          name="Conversations"
          line="Durable sessions per channel."
          tags={[`${st.sessions ?? 0} sessions`, `${st.messages ?? 0} messages`]}
          onClick={() => onNavigate('conversations')}
        />
        <PrimitiveCard
          icon={UserCheck}
          name="Approvals"
          line="Human gates. Runs pause, resume."
          tags={[`${st.approvalsPending} pending`]}
          amber={st.approvalsPending > 0}
          onClick={() => onNavigate('approvals')}
        />
        <PrimitiveCard
          icon={Zap}
          name="Events & jobs"
          line="Cron, retries, dead-letter."
          tags={[`cron ×${cronCount}`, `${st.jobsPending} queued`]}
          onClick={() => onNavigate('jobs')}
        />
        <PrimitiveCard
          icon={ListOrdered}
          name="Queue & turns"
          line="Durable jobs, any language."
          tags={['polyglot workers', 'durable streams']}
          onClick={() => onNavigate('queue')}
        />
        <PrimitiveCard
          icon={Send}
          name="Channels"
          line="Webhook, Slack, email."
          tags={state.channels.map((c) => c.type + (c.enabled ? '' : ' (off)'))}
          onClick={() => onNavigate('channels')}
        />
        <PrimitiveCard
          icon={KeySquare}
          name="Connected credentials"
          line="Vaulted, injected at call time."
          tags={[
            `${connections.length} connections`,
            `${state.tools.filter((t) => t.provider).length} scoped tools`,
          ]}
          onClick={() => onNavigate('connections')}
        />
        <PrimitiveCard
          icon={Activity}
          name="Observability"
          line="Forensic traces, cost accounting."
          tags={['traces on']}
          onClick={() => onNavigate('runs')}
        />
        <PrimitiveCard
          icon={KeyRound}
          name="Secrets"
          line="Names only, never values."
          tags={[`${state.secrets.length} keys`]}
          onClick={() => onNavigate('secrets')}
        />
      </div>

      <div className="term">
        <Window name="live state · GET /console">{contextJson}</Window>
      </div>
    </>
  )
}
