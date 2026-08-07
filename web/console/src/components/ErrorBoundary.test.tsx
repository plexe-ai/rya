import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { ErrorBoundary, RootErrorBoundary, ViewErrorBoundary } from './ErrorBoundary'

// React logs every caught render error to the console itself. That is correct in
// production and pure noise here — six deliberate throws would bury a real failure
// in the suite output — so it is silenced per-file and asserted on where it matters.
let logged: unknown[][]
beforeEach(() => {
  logged = []
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    logged.push(args)
  })
})
afterEach(() => vi.restoreAllMocks())

function Boom({ throws = true, message = 'kaboom' }: { throws?: boolean; message?: string }) {
  if (throws) throw new Error(message)
  return <div>view content</div>
}

const fallback = (error: Error, retry: () => void) => (
  <div>
    <span>caught: {error.message}</span>
    <button onClick={retry}>retry</button>
  </div>
)

describe('ErrorBoundary', () => {
  it('renders children untouched when nothing throws', () => {
    render(
      <ErrorBoundary scope="test" fallback={fallback}>
        <Boom throws={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('view content')).toBeTruthy()
    expect(logged).toHaveLength(0)
  })

  it('contains a render throw instead of unmounting the tree', () => {
    render(
      <div>
        <span>shell chrome</span>
        <ErrorBoundary scope="test" fallback={fallback}>
          <Boom />
        </ErrorBoundary>
      </div>,
    )
    expect(screen.getByText('caught: kaboom')).toBeTruthy()
    // The whole point: everything outside the boundary is still mounted.
    expect(screen.getByText('shell chrome')).toBeTruthy()
    expect(screen.queryByText('view content')).toBeNull()
  })

  it('names the boundary that caught it, which React’s own log does not', () => {
    render(
      <ErrorBoundary scope="view:runs" fallback={fallback}>
        <Boom />
      </ErrorBoundary>,
    )
    expect(logged.some((a) => String(a[0]).includes('contained by view:runs'))).toBe(true)
  })

  it('shows the component stack, which names the component that threw', () => {
    render(
      <ErrorBoundary scope="test" fallback={fallback}>
        <Boom />
      </ErrorBoundary>,
    )
    fireEvent.click(screen.getByText('Component stack'))
    expect(screen.getByText(/Boom/)).toBeTruthy()
  })

  /** The console renders hostile ids as text everywhere else; a fallback is no exception. */
  it('renders a hostile error message as text, not markup', () => {
    render(
      <ErrorBoundary scope="test" fallback={fallback}>
        <Boom message={'<img src=x onerror="alert(1)">'} />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/caught: <img src=x onerror="alert\(1\)">/)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })

  it('retries: the children get a real second chance', () => {
    function Flaky() {
      const [n, setN] = useState(0)
      return (
        <>
          <button onClick={() => setN(1)}>fix it</button>
          <ErrorBoundary scope="test" fallback={fallback}>
            <Boom throws={n === 0} />
          </ErrorBoundary>
        </>
      )
    }
    render(<Flaky />)
    expect(screen.getByText('caught: kaboom')).toBeTruthy()

    fireEvent.click(screen.getByText('fix it')) // the underlying cause goes away...
    fireEvent.click(screen.getByText('retry')) // ...and the boundary lets go.
    expect(screen.getByText('view content')).toBeTruthy()
  })

  it('resets when a resetKey changes, and stays tripped when none does', () => {
    const tree = (key: string) => (
      <ErrorBoundary scope="test" resetKeys={[key]} fallback={fallback}>
        <Boom throws={key === 'broken'} />
      </ErrorBoundary>
    )
    const { rerender } = render(tree('broken'))
    expect(screen.getByText('caught: kaboom')).toBeTruthy()

    // An unrelated re-render must NOT clear the error — that would loop, since the
    // child throws again immediately.
    rerender(tree('broken'))
    expect(screen.getByText('caught: kaboom')).toBeTruthy()

    rerender(tree('ok'))
    expect(screen.getByText('view content')).toBeTruthy()
  })
})

describe('ViewErrorBoundary', () => {
  it('offers a way out that does not require knowing the hash is the route', () => {
    const onHome = vi.fn()
    render(
      <ViewErrorBoundary view="runs" agent="acme" onHome={onHome}>
        <Boom message="Cannot read properties of null (reading 'name')" />
      </ViewErrorBoundary>,
    )
    expect(screen.getByText('This view failed to render.')).toBeTruthy()
    expect(screen.getByText("Cannot read properties of null (reading 'name')")).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /back to overview/i }))
    expect(onHome).toHaveBeenCalled()
  })

  it('clears when the agent changes, without waiting for a navigation', () => {
    const view = (agent: string) => (
      <ViewErrorBoundary view="runs" agent={agent} onHome={() => {}}>
        <Boom throws={agent === 'acme'} />
      </ViewErrorBoundary>
    )
    const { rerender } = render(view('acme'))
    expect(screen.getByText('This view failed to render.')).toBeTruthy()

    rerender(view('other-co'))
    expect(screen.getByText('view content')).toBeTruthy()
  })
})

describe('RootErrorBoundary', () => {
  it('stands alone and clears the hash before reloading', () => {
    const reload = vi.fn()
    // The hash IS the route: reloading without clearing it lands straight back on
    // the view that threw, which is what makes a white screen feel permanent.
    Object.defineProperty(window, 'location', {
      value: { ...window.location, hash: '#guard', reload },
      configurable: true,
      writable: true,
    })

    render(
      <RootErrorBoundary>
        <Boom message="shell is broken" />
      </RootErrorBoundary>,
    )
    expect(screen.getByText('The console failed to load.')).toBeTruthy()
    expect(screen.getByText('shell is broken')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /reload on overview/i }))
    expect(location.hash).toBe('overview')
    expect(reload).toHaveBeenCalled()
  })
})
