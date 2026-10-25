// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChangeList } from './ChangeList';
import { useConfirmAction, type ConfirmRequest } from './useConfirmAction';
import { writeEach } from './writeEach';

afterEach(cleanup);

function Harness({ request }: { request: Omit<ConfirmRequest, 'title' | 'confirmLabel'> }) {
  const confirmation = useConfirmAction();
  return (
    <>
      <button
        type="button"
        onClick={() =>
          confirmation.request({ title: 'Delete "alpha"?', confirmLabel: 'Delete', ...request })
        }
      >
        Delete alpha
      </button>
      {confirmation.dialog}
    </>
  );
}

describe('useConfirmAction', () => {
  it('writes nothing on Cancel and closes on a successful Confirm', async () => {
    const run = vi.fn(async () => {});
    render(<Harness request={{ body: <p>Removes alpha.</p>, run }} />);

    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete "alpha"?' });
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(run).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('blocks a second Confirm, Escape and Cancel while busy', async () => {
    let finish!: () => void;
    const run = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    render(<Harness request={{ body: null, run }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    const busy = within(dialog).getByRole('button', { name: 'Delete…' });
    expect(busy).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    fireEvent.click(busy);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(run).toHaveBeenCalledTimes(1);

    finish();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the dialog open with the error, and retries on Confirm', async () => {
    const run = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce();
    render(<Harness request={{ body: null, run }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('backend unavailable');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(run).toHaveBeenCalledTimes(2);
  });

  it('waits for the exact typed text before a typed confirmation can be confirmed', async () => {
    const run = vi.fn(async () => {});
    render(<Harness request={{ body: null, requireText: 'alpha', run }} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Delete' });
    const input = within(dialog).getByLabelText('Type alpha to confirm');

    expect(confirm).toBeDisabled();
    fireEvent.change(input, { target: { value: 'alph' } });
    fireEvent.click(confirm);
    expect(run).not.toHaveBeenCalled();

    // Tab from Confirm wraps to the details, where the typed field lives.
    fireEvent.change(input, { target: { value: 'alpha' } });
    expect(confirm).toBeEnabled();
    confirm.focus();
    fireEvent.keyDown(confirm, { key: 'Tab' });
    const details = within(dialog).getByRole('region', { name: 'Confirmation details' });
    expect(details).toHaveFocus();
    expect(details).toContainElement(input);
    fireEvent.click(confirm);
    await waitFor(() => expect(run).toHaveBeenCalledTimes(1));
  });
});

/**
 * Two writes on one confirmation: "alpha" opens at once, "beta" opens after a
 * read the test resolves (like a recipient count or a preview), and "both"
 * asks for the two in the same tick.
 */
function TwoActions({
  runAlpha,
  runBeta,
  readBeta,
}: {
  runAlpha: () => Promise<void>;
  runBeta: () => Promise<void>;
  readBeta: () => Promise<void>;
}) {
  const confirmation = useConfirmAction();
  const alpha: ConfirmRequest = {
    title: 'Delete "alpha"?',
    confirmLabel: 'Delete',
    body: <p>Removes alpha.</p>,
    run: runAlpha,
  };
  const beta: ConfirmRequest = {
    title: 'Publish "beta"?',
    confirmLabel: 'Publish',
    body: <p>Publishes beta.</p>,
    run: runBeta,
  };
  return (
    <>
      <button type="button" onClick={() => confirmation.request(alpha)}>
        Delete alpha
      </button>
      <button
        type="button"
        onClick={async () => {
          await readBeta();
          confirmation.request(beta);
        }}
      >
        Publish beta
      </button>
      <button
        type="button"
        onClick={() => {
          confirmation.request(alpha);
          confirmation.request(beta);
        }}
      >
        Both
      </button>
      {confirmation.dialog}
    </>
  );
}

describe('useConfirmAction: an open confirmation is never replaced', () => {
  function deferred() {
    let resolve!: () => void;
    const promise = new Promise<void>((done) => {
      resolve = done;
    });
    return { promise, resolve };
  }

  function setup() {
    const read = deferred();
    const runAlpha = vi.fn(async () => {});
    const runBeta = vi.fn(async () => {});
    render(<TwoActions runAlpha={runAlpha} runBeta={runBeta} readBeta={() => read.promise} />);
    return { read, runAlpha, runBeta };
  }

  it('keeps the open confirmation when a request arrives after a read, and its button runs only its own write', async () => {
    const { read, runAlpha, runBeta } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Publish beta' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete "alpha"?' });
    const confirm = within(dialog).getByRole('button', { name: 'Delete' });

    await act(async () => read.resolve());

    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Delete "alpha"?' })).toHaveTextContent(
      'Removes alpha.',
    );
    expect(confirm).toHaveTextContent('Delete');
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(runAlpha).toHaveBeenCalledTimes(1);
    expect(runBeta).not.toHaveBeenCalled();
  });

  it('refuses the second of two requests made in the same tick', async () => {
    const { runAlpha, runBeta } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Both' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete "alpha"?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(runAlpha).toHaveBeenCalledTimes(1);
    expect(runBeta).not.toHaveBeenCalled();
  });

  it('queues nothing: a refused request does not open once the confirmation closes', async () => {
    const { read, runBeta } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Publish beta' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    await act(async () => read.resolve());
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Delete "alpha"?' })).getByRole('button', {
        name: 'Cancel',
      }),
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(runBeta).not.toHaveBeenCalled();
  });

  it('keeps a failed confirmation protected until it is retried', async () => {
    const read = deferred();
    const runAlpha = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce();
    const runBeta = vi.fn(async () => {});
    render(<TwoActions runAlpha={runAlpha} runBeta={runBeta} readBeta={() => read.promise} />);
    fireEvent.click(screen.getByRole('button', { name: 'Publish beta' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete "alpha"?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('backend unavailable');

    // The late request finds the failed confirmation still open: it stays, error and all.
    await act(async () => read.resolve());
    expect(screen.getByRole('dialog', { name: 'Delete "alpha"?' })).toHaveTextContent(
      'backend unavailable',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(runAlpha).toHaveBeenCalledTimes(2);
    expect(runBeta).not.toHaveBeenCalled();
  });

  it('opens a new request after a cancel, and after a successful confirm', async () => {
    const { runAlpha } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Delete alpha' }));
    expect(screen.getByRole('dialog', { name: 'Delete "alpha"?' })).toBeInTheDocument();
    expect(runAlpha).toHaveBeenCalledTimes(1);
  });
});

describe('ChangeList', () => {
  it('lets a long value wrap instead of widening the dialog', () => {
    const url = `https://example.test/${'very-long-segment/'.repeat(20)}v1`;
    render(<ChangeList changes={[{ label: 'Base URL', before: 'https://a.test', after: url }]} />);
    const row = screen.getByText('Base URL').closest('li') as HTMLElement;
    expect(row.className).toContain('[overflow-wrap:anywhere]');
    expect(row).toHaveTextContent(`https://a.test → ${url}`);
  });
});

describe('writeEach', () => {
  it('throws when the first write fails, so nothing is reported as saved', async () => {
    await expect(
      writeEach(
        ['a', 'b'],
        (item) => item,
        async () => {
          throw new Error('down');
        },
      ),
    ).rejects.toThrow('a could not be saved: down');
  });

  it('stops at a later failure and names what was saved', async () => {
    const written: string[] = [];
    const message = await writeEach(
      ['a', 'b', 'c'],
      (item) => item,
      async (item) => {
        if (item === 'b') throw new Error('down');
        written.push(item);
      },
    );
    expect(written).toEqual(['a']);
    expect(message).toBe(
      'b could not be saved: down 1 change was saved; the rest are still unsaved.',
    );
  });

  it('returns null when every write succeeds', async () => {
    expect(
      await writeEach(
        ['a'],
        (item) => item,
        async () => {},
      ),
    ).toBeNull();
  });
});
