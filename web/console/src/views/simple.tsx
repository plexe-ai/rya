// Views that are a pure function of the `/console` aggregate — no extra fetch, no
// local state. Grouped in one file because each is a handful of lines; anything
// that grows its own loading or interaction gets promoted to its own module.

import { PERM_CLASS, stamp } from '../lib/format'
import type { ConsoleState } from '../lib/types'
import { Empty, SecRow, Table, ViewHeader, Window } from '../components/ui'

export function ManifestView({ state }: { state: ConsoleState }) {
  return (
    <>
      <ViewHeader title="Manifest">
        The declarative source of truth, read live from the running agent.
      </ViewHeader>
      <Window name="rya.agent.yaml">{state.manifestYaml || '(manifest unavailable)'}</Window>
    </>
  )
}

export function ModelsView({ state }: { state: ConsoleState }) {
  return (
    <>
      <ViewHeader title="Model gateway">
        Foundation and custom models, permissioned and versioned. Every call is logged.
      </ViewHeader>
      <Table
        rows={state.models}
        rowKey={(m) => m.id}
        columns={[
          { header: 'Model', cell: (m) => <span className="mono">{m.id}</span> },
          { header: 'Provider', cell: (m) => m.type },
          {
            header: 'Permission',
            cell: (m) => <span className={`pb ${PERM_CLASS[m.permission] ?? ''}`}>{m.permission}</span>,
          },
          { header: 'Version', cell: (m) => m.version || '—' },
          { header: 'Calls', cell: (m) => String(m.calls) },
        ]}
      />
    </>
  )
}

export function ChannelsView({ state }: { state: ConsoleState }) {
  return (
    <>
      <ViewHeader title="Channels">
        How events arrive and messages go out — one interface across surfaces.
      </ViewHeader>
      <Table
        rows={state.channels}
        rowKey={(c) => c.type}
        columns={[
          { header: 'Channel', cell: (c) => <span className="mono">{c.type}</span> },
          {
            header: 'Endpoint',
            cell: (c) => (c.path ? <span className="mono dim">{c.path}</span> : <span className="dim">—</span>),
          },
          {
            header: 'Status',
            cell: (c) =>
              c.enabled ? (
                <span className="stbadge ok">
                  <span className="d" />
                  enabled
                </span>
              ) : (
                <span className="stbadge">
                  <span className="d" />
                  <span className="dim">disabled</span>
                </span>
              ),
          },
        ]}
      />
    </>
  )
}

export function SecretsView({ state }: { state: ConsoleState }) {
  return (
    <>
      <ViewHeader title="Secrets">Names only, never values — they never reach the agent.</ViewHeader>
      <Table
        rows={state.secrets}
        rowKey={(n) => n}
        emptyMessage="No secrets configured."
        columns={[
          { header: 'Name', cell: (n) => <span className="mono">{n}</span> },
          { header: 'Value', cell: () => <span className="mono dim">••••••••••••</span> },
          { header: 'Source', cell: () => <span className="dim">.env / Secrets Manager</span> },
        ]}
      />
    </>
  )
}

export function MemoryView({ state }: { state: ConsoleState }) {
  const blocks = state.memory.blocks ?? []
  return (
    <>
      <ViewHeader title="Memory">
        Durable, scoped state — addressable by the agent, isolated per user by row-level security.
      </ViewHeader>

      <SecRow
        left="Core memory blocks"
        right={`always in context · self-editable · ${blocks.length}`}
      />
      <Table
        rows={blocks}
        rowKey={(b) => b.name}
        emptyMessage="No core blocks — set with ctx.memory.block_set()."
        columns={[
          { header: 'Block', cell: (b) => <span className="mono">{b.name}</span> },
          { header: 'Size', cell: (b) => `${b.chars} ch` },
          { header: 'Limit', cell: (b) => `${b.limit} ch` },
          { header: 'Updated', cell: (b) => <span className="dim">{stamp(b.updatedAt)}</span> },
        ]}
      />

      <SecRow left="Long-term memory" right="consolidated facts + vector recall" />
      <Table
        rows={state.memory.collections}
        rowKey={(c) => c.name}
        emptyMessage="No long-term memory yet — write with ctx.memory.remember()."
        columns={[
          { header: 'Collection', cell: (c) => <span className="mono">{c.name}</span> },
          { header: 'Items', cell: (c) => String(c.count) },
          { header: 'Scope', cell: () => 'per-agent' },
          {
            header: 'Recall',
            cell: (c) =>
              c.name === 'facts' ? (
                <span className="pb allowed">semantic</span>
              ) : (
                <span className="dim">—</span>
              ),
          },
        ]}
      />
    </>
  )
}

export function JobsView({ state }: { state: ConsoleState }) {
  const cron = state.triggers.filter((t) => t.type === 'cron')
  const st = state.stats
  return (
    <>
      <ViewHeader title="Jobs &amp; cron">
        Background work the runtime owns — schedules, retries with backoff, and a dead-letter queue.
      </ViewHeader>
      {cron.length === 0 && !st.jobsPending ? (
        <Empty>No jobs or schedules.</Empty>
      ) : (
        <>
          <Table
            rows={cron}
            rowKey={(t) => t.id}
            emptyMessage="No schedules."
            columns={[
              { header: 'Schedule', cell: (t) => <span className="mono">{t.id}</span> },
              { header: 'Type', cell: () => 'cron' },
              { header: 'Detail', cell: (t) => <span className="mono">{t.schedule}</span> },
              {
                header: 'Status',
                cell: () => (
                  <span className="stbadge ok">
                    <span className="d" />
                    scheduled
                  </span>
                ),
              },
            ]}
          />
          <SecRow left="Queue" right={`${st.jobsPending} pending`} />
        </>
      )}
    </>
  )
}

/**
 * Placeholder for a view that still lives in the legacy console. Being explicit
 * beats an empty panel: an operator who lands here needs to know the data exists
 * and where to find it, not wonder whether the page is broken.
 */
