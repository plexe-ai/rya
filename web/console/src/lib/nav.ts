import {
  Activity, BookOpenText, Bot, Clock, Cpu, Database, FileCog, FlaskConical, Gauge,
  GitBranch, KeyRound, KeySquare, Layers, LayoutDashboard, ListOrdered, MessagesSquare,
  Package, Plug, ScanLine, Send, Server, ShieldCheck, ShieldHalf, UserCheck, Users,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

// The nav is data, not markup. In the legacy console adding a view meant editing
// four places (the `<nav>` HTML, the `views` array, the `NAVBTNS` selector, and
// the `show()` switch); here a view is one entry plus one component.

export type ViewId =
  | 'overview' | 'infra' | 'manifest' | 'tools' | 'memory' | 'knowledge' | 'models'
  | 'channels' | 'connections'
  | 'deploy' | 'versions' | 'workers' | 'quotas'
  | 'runs' | 'conversations' | 'evals' | 'jobs' | 'queue'
  | 'governance' | 'approvals' | 'guard' | 'secrets' | 'team'

/** Sidebar count keys, kept as a union so a typo is a build error. */
export type CountKey =
  | 'tools' | 'memory' | 'knowledge' | 'models' | 'channels' | 'connections'
  | 'envs' | 'versions' | 'workers' | 'sessions' | 'queue'
  | 'violations' | 'approvals' | 'secrets'

export interface NavItem {
  id: ViewId
  label: string
  icon: LucideIcon
  count?: CountKey
  /** Render the count in amber when non-zero (attention, not decoration). */
  amberCount?: boolean
}

export interface NavGroup {
  title: string
  items: NavItem[]
}

export const NAV: NavGroup[] = [
  {
    title: 'Build',
    items: [
      { id: 'overview', label: 'Overview', icon: LayoutDashboard },
      { id: 'infra', label: 'Infrastructure', icon: Server },
      { id: 'manifest', label: 'Manifest', icon: FileCog },
      { id: 'tools', label: 'Tools', icon: Plug, count: 'tools' },
      { id: 'memory', label: 'Memory', icon: Database, count: 'memory' },
      { id: 'knowledge', label: 'Knowledge', icon: BookOpenText, count: 'knowledge' },
      { id: 'models', label: 'Models', icon: Layers, count: 'models' },
      { id: 'channels', label: 'Channels', icon: Send, count: 'channels' },
      { id: 'connections', label: 'Connections', icon: KeySquare, count: 'connections' },
    ],
  },
  {
    title: 'Deploy',
    items: [
      { id: 'deploy', label: 'Environments', icon: GitBranch, count: 'envs' },
      { id: 'versions', label: 'Versions', icon: Package, count: 'versions' },
      { id: 'workers', label: 'Workers', icon: Cpu, count: 'workers' },
      { id: 'quotas', label: 'Quota & usage', icon: Gauge },
    ],
  },
  {
    title: 'Operate',
    items: [
      { id: 'runs', label: 'Runs & traces', icon: ScanLine },
      { id: 'conversations', label: 'Conversations', icon: MessagesSquare, count: 'sessions' },
      { id: 'evals', label: 'Evals', icon: FlaskConical },
      { id: 'jobs', label: 'Jobs & cron', icon: Clock },
      { id: 'queue', label: 'Queue & turns', icon: ListOrdered, count: 'queue' },
    ],
  },
  {
    title: 'Govern',
    items: [
      { id: 'governance', label: 'Governance', icon: ShieldCheck, count: 'violations', amberCount: true },
      { id: 'approvals', label: 'Approvals', icon: UserCheck, count: 'approvals', amberCount: true },
      { id: 'guard', label: 'Action Guard', icon: ShieldHalf },
      { id: 'secrets', label: 'Secrets', icon: KeyRound, count: 'secrets' },
      { id: 'team', label: 'Team & access', icon: Users },
    ],
  },
]

export const ALL_VIEWS: ViewId[] = NAV.flatMap((g) => g.items.map((i) => i.id))

/** Icons re-exported for views that need them outside the nav. */
export { Bot, Activity }
