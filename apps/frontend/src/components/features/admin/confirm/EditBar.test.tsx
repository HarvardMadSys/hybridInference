// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EditBar } from './EditBar';

afterEach(cleanup);

describe('EditBar', () => {
  it('reads "Saving…" for its own save, and holds both buttons', () => {
    render(<EditBar target="weights" changeCount={1} onCancel={vi.fn()} onSave={vi.fn()} busy />);
    const save = screen.getByRole('button', { name: 'Save changes to weights' });
    expect(save).toHaveTextContent('Saving…');
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel editing weights' })).toBeDisabled();
  });

  it('waits while another write runs, without saying that it saves', () => {
    const onSave = vi.fn();
    const onCancel = vi.fn();
    render(
      <EditBar target="weights" changeCount={2} onCancel={onCancel} onSave={onSave} held inPlace />,
    );
    const save = screen.getByRole('button', { name: 'Save changes to weights' });
    expect(save).toHaveTextContent('Save changes');
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard changes to weights' })).toBeDisabled();
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    fireEvent.click(save);
    expect(onSave).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });
});
