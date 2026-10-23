// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from './ConfirmDialog';

afterEach(cleanup);

describe('ConfirmDialog', () => {
  it('keeps Cancel as the initial focus and includes scrollable details in the focus loop', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="Save configuration changes?"
        confirmLabel="Save"
        onConfirm={onConfirm}
        onCancel={onCancel}
      >
        <p>A long configuration change to review before saving.</p>
      </ConfirmDialog>,
    );

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const save = screen.getByRole('button', { name: 'Save' });
    const details = screen.getByRole('region', { name: 'Confirmation details' });
    expect(cancel).toHaveFocus();

    // A keyboard user can reach and scroll the details instead of being
    // trapped between the two action buttons.
    save.focus();
    fireEvent.keyDown(save, { key: 'Tab' });
    expect(details).toHaveFocus();
    fireEvent.keyDown(details, { key: 'Tab', shiftKey: true });
    expect(save).toHaveFocus();

    // Reading details must not submit, and Escape still cancels from there.
    details.focus();
    fireEvent.keyDown(details, { key: 'Escape' });
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
