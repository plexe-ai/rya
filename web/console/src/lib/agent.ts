/**
 * Agent-scoped request paths.
 *
 * Since D21 one deployment serves many agents, so every agent-scoped route has to
 * name which one. The unprefixed spellings still answer via the deprecated Rule 6
 * fallback, which resolves the reserved `_` alias — and that alias **refuses**
 * (`E_AGENT_AMBIGUOUS`, 400) rather than guessing the moment a workspace serves a
 * second agent. So a hard-coded `/runs` or `/environments` is not a shortcut, it is a
 * bug with a delayed fuse: it works on a one-agent workspace and breaks on the day
 * someone publishes a second.
 *
 * Views take `state: ConsoleState`, which always carries the selected agent, so the
 * name is threaded through the argument rather than through a context or a module
 * global — one fewer piece of hidden state, and it makes the dependency visible at
 * every call site:
 *
 *     const { data } = useLoad(() => api<Envs>(ag(state.agent.name, '/environments')))
 */
export function ag(agent: string, path: string): string {
  return `/agents/${encodeURIComponent(agent)}${path}`
}

/** Where the selected agent is remembered. Shared with the legacy console on purpose. */
export const AGENT_KEY = 'rya_agent'

export const readAgent = (): string | null => localStorage.getItem(AGENT_KEY) || null

export function writeAgent(name: string | null): void {
  if (name) localStorage.setItem(AGENT_KEY, name)
  else localStorage.removeItem(AGENT_KEY)
}
