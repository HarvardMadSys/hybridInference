// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import { DragScrollArea } from './DragScrollArea';

afterEach(() => {
  cleanup();
});

/** Make the rendered wrapper report horizontal overflow so the drag path engages. */
function forceOverflow(wrapper: HTMLElement): void {
  Object.defineProperty(wrapper, 'scrollWidth', { configurable: true, value: 500 });
  Object.defineProperty(wrapper, 'clientWidth', { configurable: true, value: 100 });
}

/**
 * Render a non-interactive content element (mirrors RecentRequests' clickable
 * `<tr>`, which is not an interactive element per isInteractiveTarget) with a
 * native click listener so we can assert whether a gesture reached it.
 */
function renderWithClickTarget(): { child: HTMLElement; onClick: ReturnType<typeof vi.fn> } {
  const { container } = render(
    <DragScrollArea>
      <div>row</div>
    </DragScrollArea>,
  );
  forceOverflow(container.firstElementChild as HTMLElement);
  const child = screen.getByText('row');
  const onClick = vi.fn();
  child.addEventListener('click', onClick);
  return { child, onClick };
}

describe('DragScrollArea', () => {
  it('lets a plain tap reach the content click (does not capture on pointerdown)', () => {
    const { child, onClick } = renderWithClickTarget();

    // A tap: pointer down and up at the same spot, followed by the click the
    // browser synthesizes. Capture must not be taken, so the click passes through.
    fireEvent.pointerDown(child, { pointerId: 1, pointerType: 'touch', button: 0, clientX: 10 });
    fireEvent.pointerUp(child, { pointerId: 1, pointerType: 'touch', clientX: 10 });
    fireEvent.click(child);

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('suppresses the click when the gesture is a horizontal drag', () => {
    const { child, onClick } = renderWithClickTarget();

    fireEvent.pointerDown(child, { pointerId: 1, pointerType: 'touch', button: 0, clientX: 10 });
    // Move well past the drag threshold -> becomes a drag.
    fireEvent.pointerMove(child, { pointerId: 1, pointerType: 'touch', clientX: 60 });
    fireEvent.pointerUp(child, { pointerId: 1, pointerType: 'touch', clientX: 60 });
    fireEvent.click(child);

    expect(onClick).not.toHaveBeenCalled();
  });

  it('does not drag-scroll for mouse input (preserves native text selection)', () => {
    const { child, onClick } = renderWithClickTarget();

    // A mouse drag must NOT be treated as a scroll gesture, so the click is left
    // intact and native text selection is preserved.
    fireEvent.pointerDown(child, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 10 });
    fireEvent.pointerMove(child, { pointerId: 1, pointerType: 'mouse', clientX: 60 });
    fireEvent.pointerUp(child, { pointerId: 1, pointerType: 'mouse', clientX: 60 });
    fireEvent.click(child);

    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
