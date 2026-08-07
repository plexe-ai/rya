import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { RotateCw, TriangleAlert } from 'lucide-react'

// React 19 does not degrade on an uncaught render error — it unmounts the WHOLE
// tree. No sidebar, no nav, no toast: a blank page. And because the hash is the
// route, reloading lands back on the view that threw, so the console is bricked
// until someone knows to edit the address bar.
//
// The legacy console could not fail this way. It rendered strings into innerHTML,
// so a bad dereference threw inside one handler, got caught, and became a toast
// over an otherwise working page. The screenshot at the repo root — "Cannot read
// properties of null (reading 'name')" — is exactly that, surviving. Trading string
// templates for a component tree bought escaping and types and cost us that
// containment; a boundary buys it back.
//
// There is no function-component equivalent: `getDerivedStateFromError` and
// `componentDidCatch` are class-only in React 19, which is why this one file is a
// class. `onUncaughtError` on `createRoot` observes, it does not contain.

interface Props {
  children: ReactNode
  /**
   * Re-render the children when any of these change while the boundary is tripped.
   *
   * Deliberately NOT a `key` on the boundary itself: a `key` would remount healthy
   * children on every navigation too, and the shell already gives each view its own
   * component identity. This resets the *error*, and only when there is one.
   */
  resetKeys?: readonly unknown[]
  /** Names what failed, for the log line and the fallback copy. */
  scope: string
  fallback: (error: Error, retry: () => void) => ReactNode
}

interface State {
  error: Error | null
  /** React's component stack — names the view that threw, unlike the JS stack. */
  stack: string | null
}

const sameKeys = (a: readonly unknown[] = [], b: readonly unknown[] = []) =>
  a.length === b.length && a.every((v, i) => Object.is(v, b[i]))

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, stack: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ stack: info.componentStack ?? null })
    // React logs the error itself; this adds the one thing its log lacks — which
    // boundary caught it, so "the whole console died" and "the Runs table died"
    // are distinguishable in a copy-pasted console dump from an operator.
    console.error(`[rya] render error contained by ${this.props.scope}:`, error)
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && !sameKeys(prev.resetKeys, this.props.resetKeys)) {
      this.setState({ error: null, stack: null })
    }
  }

  retry = () => this.setState({ error: null, stack: null })

  render() {
    const { error, stack } = this.state
    if (!error) return this.props.children
    return (
      <>
        {this.props.fallback(error, this.retry)}
        {stack && <StackDetails stack={stack} />}
      </>
    )
  }
}

/**
 * The component stack, collapsed.
 *
 * Worth showing rather than hiding behind a support request: it names the view and
 * the component, and `sourcemap: false` in the production build means the JS stack
 * alone would be minified noise. Rendered as text, never as markup.
 */
function StackDetails({ stack }: { stack: string }) {
  return (
    <details style={{ marginTop: 12 }}>
      <summary className="dim" style={{ cursor: 'pointer', fontSize: 12.5 }}>
        Component stack
      </summary>
      <pre className="mono dim" style={{ fontSize: 11.5, whiteSpace: 'pre-wrap', margin: '8px 0 0' }}>
        {stack.trim()}
      </pre>
    </details>
  )
}

/**
 * The per-view boundary. The shell — sidebar, top bar, agent picker — stays mounted,
 * so the operator can navigate away from a broken view instead of reloading into it.
 */
export function ViewErrorBoundary({
  view,
  agent,
  onHome,
  children,
}: {
  view: string
  /** Reset on an agent switch too: the new agent's data may well render fine. */
  agent: string | null
  onHome: () => void
  children: ReactNode
}) {
  return (
    <ErrorBoundary
      scope={`view:${view}`}
      resetKeys={[view, agent]}
      fallback={(error, retry) => (
        <div className="empty" style={{ textAlign: 'left' }}>
          <TriangleAlert aria-hidden="true" focusable="false" />
          <div style={{ textAlign: 'center' }}>
            <strong>This view failed to render.</strong>
            <div style={{ marginTop: 6 }}>
              The rest of the console is unaffected — the data below may be missing or
              malformed, not the runtime.
            </div>
            {/* As text. An error message can carry an id straight from the server. */}
            <div className="mono" style={{ marginTop: 10, color: 'var(--text-2)', fontSize: 12.5 }}>
              {error.message || String(error)}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 14 }}>
              <button className="btn sm" onClick={retry}>
                <RotateCw aria-hidden="true" focusable="false" />
                Try again
              </button>
              <button className="btn sm" onClick={onHome}>
                Back to overview
              </button>
            </div>
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  )
}

/**
 * The last resort, in `main.tsx`, around the shell itself.
 *
 * Nothing above it is mounted, so this cannot lean on `.app`, the sidebar or the
 * toast — it renders standalone and its only actions are ones that work without any
 * app state. It never touches the stored token: a render bug is not a reason to sign
 * an operator out of a governance console.
 */
export function RootErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <ErrorBoundary
      scope="root"
      fallback={(error, retry) => (
        <div className="wrap">
          <div className="empty" style={{ marginTop: 48 }}>
            <TriangleAlert aria-hidden="true" focusable="false" />
            <strong>The console failed to load.</strong>
            <div style={{ marginTop: 6 }}>
              This is a bug in the console, not an outage — the runtime and your agents are
              unaffected.
            </div>
            <div className="mono" style={{ marginTop: 10, color: 'var(--text-2)', fontSize: 12.5 }}>
              {error.message || String(error)}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 14 }}>
              <button className="btn sm" onClick={retry}>
                <RotateCw aria-hidden="true" focusable="false" />
                Try again
              </button>
              {/* The hash IS the route, so a reload alone would land straight back on
                  the view that threw. Clear it first. */}
              <button
                className="btn sm dark"
                onClick={() => {
                  location.hash = 'overview'
                  location.reload()
                }}
              >
                Reload on Overview
              </button>
            </div>
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  )
}
