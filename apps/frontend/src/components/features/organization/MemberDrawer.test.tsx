// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemberDrawer } from './MemberDrawer';

afterEach(cleanup);

function open(props: Partial<Parameters<typeof MemberDrawer>[0]> = {}) {
  const onClose = vi.fn();
  render(
    <MemberDrawer title="Lei Wang" subtitle="lei.wang@example.edu" onClose={onClose} {...props}>
      <button type="button">Set monthly limit</button>
      <button type="button">Remove from organization</button>
    </MemberDrawer>,
  );
  return { panel: screen.getByRole('dialog', { name: props.title ?? 'Lei Wang' }), onClose };
}

/** A click as a person makes it: the button goes down and up on the same element. */
function press(element: HTMLElement) {
  fireEvent.mouseDown(element);
  fireEvent.mouseUp(element);
  fireEvent.click(element);
}

/** A page with a row that opens the panel, as the members table does. */
function Page() {
  const [shown, setShown] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setShown(true)}>
        Lei Wang
      </button>
      {shown ? (
        <MemberDrawer title="Lei Wang" onClose={() => setShown(false)}>
          <p>the details</p>
        </MemberDrawer>
      ) : null}
    </>
  );
}

describe('the panel of a member', () => {
  it('is a modal layer named after the member, with the email under the name', () => {
    const { panel } = open();

    expect(panel).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('heading', { name: 'Lei Wang' })).toBeInTheDocument();
    expect(panel).toHaveTextContent('lei.wang@example.edu');
    expect(screen.getByRole('button', { name: 'Set monthly limit' })).toBeInTheDocument();
  });

  it('shows no second line for a member that has only an email', () => {
    const { panel } = open({ title: 'bo.huang@example.edu', subtitle: null });

    expect(panel.querySelector('header p')).toBeNull();
  });

  it('closes on Close, on Escape, and on a click on the dimmed list', () => {
    const { panel, onClose } = open();

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);

    press(panel.parentElement as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('stays open when a press that began inside it ends on the dimmed list', () => {
    const { panel, onClose } = open();
    const dimmed = panel.parentElement as HTMLElement;

    // The end of a selection of text: the button goes down on the email and up
    // outside the panel. The browser sends that click to what holds both.
    fireEvent.mouseDown(screen.getByText('lei.wang@example.edu'));
    fireEvent.mouseUp(dimmed);
    fireEvent.click(dimmed);

    expect(onClose).not.toHaveBeenCalled();

    // A press on the dimmed list itself still closes it.
    press(dimmed);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stays open on a click inside it', () => {
    const { panel, onClose } = open();

    fireEvent.click(panel);
    fireEvent.click(screen.getByRole('button', { name: 'Set monthly limit' }));

    expect(onClose).not.toHaveBeenCalled();
  });

  it('takes the focus when it opens and gives it back to the row when it closes', () => {
    render(<Page />);
    const row = screen.getByRole('button', { name: 'Lei Wang' });
    row.focus();
    fireEvent.click(row);

    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(row).toHaveFocus();
  });

  it('keeps Tab inside, from the last control to the first and back', () => {
    open();
    const close = screen.getByRole('button', { name: 'Close' });
    const last = screen.getByRole('button', { name: 'Remove from organization' });

    last.focus();
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(close).toHaveFocus();

    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();
  });

  it('brings a focus that left it back inside', () => {
    render(
      <>
        <button type="button">behind</button>
        <MemberDrawer title="Lei Wang" onClose={vi.fn()}>
          <button type="button">Set monthly limit</button>
        </MemberDrawer>
      </>,
    );
    screen.getByRole('button', { name: 'behind' }).focus();

    fireEvent.keyDown(window, { key: 'Tab' });

    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();
  });

  it('leaves the keys to a confirmation that is open over it', () => {
    const { onClose } = open({ suspended: true });
    const last = screen.getByRole('button', { name: 'Remove from organization' });
    last.focus();

    // Escape closes the confirmation, not the panel under it.
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();

    // And the Tab order is the confirmation's.
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(last).toHaveFocus();
  });
});
