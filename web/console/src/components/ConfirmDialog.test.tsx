import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ConfirmDialog } from './ConfirmDialog'

// The contract under test is "nothing happens unless the confirm button is pressed",
// plus the a11y behaviour AuthModal already establishes for a dialog in this tree.
// Every assertion below is about one of those two, because those are the two things a
// caller is entitled to assume when it puts a destructive action behind this.

const setup = (props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) => {
  const onConfirm = vi.fn()
  const onCancel = vi.fn()
  const utils = render(
    <ConfirmDialog
      title="Delete the thing?"
      body="This cannot be undone."
      confirmLabel="Delete"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...props}
    />,
  )
  return { onConfirm, onCancel, ...utils }
}

describe('ConfirmDialog', () => {
  it('is a labelled modal dialog', () => {
    setup()
    const dialog = screen.getByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    // Named by its own heading rather than by a hardcoded id, so two of these mounted
    // at once cannot label each other.
    expect(dialog.getAttribute('aria-labelledby')).toBe(screen.getByRole('heading').id)
    expect(dialog.getAttribute('aria-describedby')).toBe(screen.getByText('This cannot be undone.').id)
  })

  it('confirms only when the confirm button is pressed', () => {
    const { onConfirm, onCancel } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('cancels on the Cancel button, and does NOT confirm', () => {
    const { onConfirm, onCancel } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('cancels on Escape, and does NOT confirm', () => {
    const { onConfirm, onCancel } = setup()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    // The whole point: the dismissal gesture an operator reaches for by reflex must
    // never be the one that performs the action.
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('stops listening for Escape once unmounted', () => {
    const { onCancel, unmount } = setup()
    unmount()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('blocks the confirm button while the caller says the decision is incomplete', () => {
    const { onConfirm } = setup({ confirmDisabled: true })
    const btn = screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('locks confirm while busy but leaves Cancel usable', () => {
    const { onCancel } = setup({ busy: true })
    expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(true)
    // A request that hangs must not also trap the operator in the dialog.
    const cancel = screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
    expect(cancel.disabled).toBe(false)
    fireEvent.click(cancel)
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('focuses the first field on open', () => {
    setup({ children: <input aria-label="Reason" /> })
    expect(document.activeElement).toBe(screen.getByLabelText('Reason'))
  })

  it('focuses Cancel, never the destructive button, when there is no field', () => {
    setup({ danger: true })
    // The click that opened this dialog is often followed by a stray Enter.
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }))
  })

  it('traps Tab inside the card, skipping a disabled confirm', () => {
    setup({ children: <input aria-label="Reason" />, confirmDisabled: true })
    const field = screen.getByLabelText('Reason')
    const cancel = screen.getByRole('button', { name: 'Cancel' })

    // Cancel is the last ENABLED stop, because the confirm button is blocked; Tab
    // from it must wrap to the field rather than escaping to the page behind.
    cancel.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(field)

    // ...and Shift+Tab off the first goes the other way.
    field.focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(cancel)
  })

  it('renders a hostile title and body as text', () => {
    const nasty = '<img src=x onerror=alert(1)>'
    setup({ title: nasty, body: nasty })
    expect(screen.getAllByText(nasty).length).toBe(2)
    expect(document.querySelector('img')).toBeNull()
  })

  it('marks a destructive confirm so it does not look like an ordinary submit', () => {
    setup({ danger: true })
    expect(screen.getByRole('button', { name: 'Delete' }).className).toContain('danger')
  })

  it('does not mark a merely consequential confirm as destructive', () => {
    // Colour is for meaning here: if every confirm were red, red would say nothing.
    setup()
    expect(screen.getByRole('button', { name: 'Delete' }).className).not.toContain('danger')
  })
})
