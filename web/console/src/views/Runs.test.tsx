import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { RunsView } from './Runs'
import type { ConsoleState, Run } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: vi.fn(() => Promise.resolve({ trace: [] })),
}))

const run = (id: string, status: Run['status'], trigger = 'message.received'): Run => ({
  id,
  status,
  trigger,
  tokens: 100,
  createdAt: '2026-08-01T11:00:00Z',
})

/** Only the fields RunsView reads; the rest of the aggregate is irrelevant here. */
const stateWith = (runs: Run[]) => ({ runs }) as unknown as ConsoleState

describe('RunsView', () => {
  it('lists every run by default', () => {
    render(<RunsView state={stateWith([run('run_a', 'completed'), run('run_b', 'failed')])} onToast={() => {}} />)
    expect(screen.getByText('run_a')).toBeTruthy()
    expect(screen.getByText('run_b')).toBeTruthy()
  })

  it('narrows the table when a status pill is chosen', () => {
    render(<RunsView state={stateWith([run('run_a', 'completed'), run('run_b', 'failed')])} onToast={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /failed/i }))
    expect(screen.queryByText('run_a')).toBeNull()
    expect(screen.getByText('run_b')).toBeTruthy()
  })

  it('narrows the table as the query is typed', () => {
    render(<RunsView state={stateWith([run('run_a', 'completed'), run('run_b', 'failed')])} onToast={() => {}} />)
    fireEvent.change(screen.getByLabelText('Filter runs'), { target: { value: 'run_b' } })
    expect(screen.queryByText('run_a')).toBeNull()
    expect(screen.getByText('run_b')).toBeTruthy()
  })

  it('explains an empty result differently when runs exist vs when none do', () => {
    const { unmount } = render(<RunsView state={stateWith([])} onToast={() => {}} />)
    expect(screen.getByText(/No runs yet/)).toBeTruthy()
    unmount()

    render(<RunsView state={stateWith([run('run_a', 'completed')])} onToast={() => {}} />)
    fireEvent.change(screen.getByLabelText('Filter runs'), { target: { value: 'zzz' } })
    expect(screen.getByText(/No runs match this filter/)).toBeTruthy()
  })

  /**
   * The regression this whole migration exists to prevent.
   *
   * The legacy console re-rendered the filter block from an HTML string on every
   * 6s poll, which blew away the search input's value and caret. It worked around
   * that by sniffing `document.activeElement` and skipping the re-render while the
   * box had focus (`renderRuns`, and the warning in console/AGENTS.md).
   *
   * Here the poll arrives as new props. The input is React-controlled and the rows
   * are keyed by run id, so a data update cannot disturb focus, value, or caret —
   * and no activeElement sniffing is needed anywhere.
   */
  it('keeps focus, value and caret in the filter box when polled data arrives', () => {
    const initial = [run('run_a', 'completed'), run('run_b', 'failed')]
    const { rerender } = render(<RunsView state={stateWith(initial)} onToast={() => {}} />)

    const input = screen.getByLabelText('Filter runs') as HTMLInputElement
    input.focus()
    fireEvent.change(input, { target: { value: 'run_' } })
    input.setSelectionRange(2, 2)

    expect(document.activeElement).toBe(input)

    // A poll lands: a brand-new run appears and an existing one changes status.
    rerender(
      <RunsView
        state={stateWith([run('run_c', 'running'), run('run_a', 'waiting_approval'), run('run_b', 'failed')])}
        onToast={() => {}}
      />,
    )

    const after = screen.getByLabelText('Filter runs') as HTMLInputElement
    expect(document.activeElement).toBe(after)
    expect(after.value).toBe('run_')
    expect(after.selectionStart).toBe(2)

    // ...and the new data really did render through the still-active filter.
    expect(screen.getByText('run_c')).toBeTruthy()
    expect(screen.getByText('waiting_approval')).toBeTruthy()
  })

  it('keeps the chosen status pill across a poll', () => {
    const { rerender } = render(
      <RunsView state={stateWith([run('run_a', 'completed'), run('run_b', 'failed')])} onToast={() => {}} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /failed/i }))

    rerender(
      <RunsView
        state={stateWith([run('run_a', 'completed'), run('run_b', 'failed'), run('run_c', 'completed')])}
        onToast={() => {}}
      />,
    )

    // run_c is completed, so the active 'failed' filter must still exclude it.
    expect(screen.queryByText('run_c')).toBeNull()
    expect(screen.getByText('run_b')).toBeTruthy()
  })

  it('renders run ids as text, so a hostile id cannot inject markup', () => {
    // The legacy renderer interpolated ids into HTML strings and relied on esc().
    // React escapes text children, so this is structural rather than a discipline.
    const nasty = '<img src=x onerror=alert(1)>'
    render(<RunsView state={stateWith([run(nasty, 'completed')])} onToast={() => {}} />)
    expect(screen.getByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
