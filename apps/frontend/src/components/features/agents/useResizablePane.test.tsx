// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PaneResizer } from './PaneResizer';
import {
  announcePaneResize,
  useResizablePane,
  type ResizablePaneOptions,
} from './useResizablePane';

const STORAGE_KEY = 'test.pane.width';

function Harness({ options }: { options?: Partial<ResizablePaneOptions> }) {
  const pane = useResizablePane({
    storageKey: STORAGE_KEY,
    defaultWidth: 300,
    minWidth: 200,
    maxWidth: 500,
    side: 'start',
    ...options,
  });
  return (
    <div>
      <div data-testid="pane" style={{ width: pane.width }} />
      <PaneResizer pane={pane} label="Resize pane" />
    </div>
  );
}

/** Same harness, but sized against a container whose layout width is stubbed. */
function ContainerHarness({
  containerWidth,
  siblingMinWidth,
}: {
  containerWidth: number;
  siblingMinWidth: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pane = useResizablePane({
    storageKey: STORAGE_KEY,
    defaultWidth: 300,
    minWidth: 200,
    maxWidth: 800,
    side: 'start',
    containerRef,
    siblingMinWidth,
  });
  return (
    <div
      ref={(node) => {
        containerRef.current = node;
        if (node)
          Object.defineProperty(node, 'clientWidth', { configurable: true, value: containerWidth });
      }}
    >
      <div data-testid="pane" style={{ width: pane.width }} />
      <PaneResizer pane={pane} label="Resize pane" />
    </div>
  );
}

function handle(): HTMLElement {
  return screen.getByRole('separator', { name: 'Resize pane' });
}

function widthOf(): string {
  return screen.getByTestId('pane').style.width;
}

function drag(from: number, to: number) {
  fireEvent.pointerDown(handle(), { button: 0, clientX: from });
  fireEvent.pointerMove(window, { clientX: to });
  fireEvent.pointerUp(window, { clientX: to });
}

describe('useResizablePane', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.innerWidth = 1400;
  });

  afterEach(() => {
    cleanup();
  });

  it('drags a start-side pane wider and reports the width to assistive tech', () => {
    render(<Harness />);

    expect(widthOf()).toBe('300px');
    drag(300, 380);

    expect(widthOf()).toBe('380px');
    expect(handle()).toHaveAttribute('aria-valuenow', '380');
    expect(handle()).toHaveAttribute('aria-valuemin', '200');
    expect(handle()).toHaveAttribute('aria-valuemax', '500');
  });

  it('grows an end-side pane when the pointer moves the other way', () => {
    render(<Harness options={{ side: 'end' }} />);

    drag(800, 720);

    expect(widthOf()).toBe('380px');
  });

  it('clamps the drag to the pane bounds', () => {
    render(<Harness />);

    drag(300, 0);
    expect(widthOf()).toBe('200px');

    drag(300, 1200);
    expect(widthOf()).toBe('500px');
  });

  it('persists the width on release and restores it on the next mount', () => {
    render(<Harness />);
    drag(300, 420);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('420');

    cleanup();
    render(<Harness />);

    expect(widthOf()).toBe('420px');
  });

  it('ignores a stored width that is not a usable number', () => {
    window.localStorage.setItem(STORAGE_KEY, 'wide');
    render(<Harness />);

    expect(widthOf()).toBe('300px');
  });

  it('nudges with the arrow keys and jumps with Home/End', () => {
    render(<Harness />);

    fireEvent.keyDown(handle(), { key: 'ArrowRight' });
    expect(widthOf()).toBe('316px');

    fireEvent.keyDown(handle(), { key: 'ArrowLeft', shiftKey: true });
    expect(widthOf()).toBe('252px');

    fireEvent.keyDown(handle(), { key: 'End' });
    expect(widthOf()).toBe('500px');

    fireEvent.keyDown(handle(), { key: 'Home' });
    expect(widthOf()).toBe('200px');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('200');
  });

  it('reverses the arrow keys for an end-side pane', () => {
    render(<Harness options={{ side: 'end' }} />);

    fireEvent.keyDown(handle(), { key: 'ArrowLeft' });

    expect(widthOf()).toBe('316px');
  });

  it('resets to the default width on double-click', () => {
    render(<Harness />);
    drag(300, 460);
    expect(widthOf()).toBe('460px');

    fireEvent.doubleClick(handle());

    expect(widthOf()).toBe('300px');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('300');
  });

  it('re-clamps to the viewport share when the window narrows, without forgetting the choice', () => {
    render(<Harness />);
    drag(300, 480);
    expect(widthOf()).toBe('480px');

    window.innerWidth = 400;
    fireEvent(window, new Event('resize'));

    expect(widthOf()).toBe('240px');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('480');

    window.innerWidth = 1400;
    fireEvent(window, new Event('resize'));

    expect(widthOf()).toBe('480px');
  });

  it('restores a stored width that did not fit the layout at mount', () => {
    // The shell can hydrate before it has its real size; a width chosen in a
    // wide window must not ratchet down to whatever fits that first frame.
    window.localStorage.setItem(STORAGE_KEY, '460');
    window.innerWidth = 600;

    render(<Harness />);
    expect(widthOf()).toBe('360px');

    window.innerWidth = 1400;
    fireEvent(window, new Event('resize'));

    expect(widthOf()).toBe('460px');
  });

  it('stops the drag where the pane across the seam hits its floor', () => {
    // Container 900 wide with a 400 floor for the other pane: 500 is the ceiling
    // even though maxWidth would allow 500+ and the window would allow 840.
    render(<ContainerHarness containerWidth={900} siblingMinWidth={400} />);

    drag(400, 900);

    expect(widthOf()).toBe('500px');
    // The announced maximum is the one the drag honours, not the configured 800.
    expect(handle()).toHaveAttribute('aria-valuemax', '500');
  });

  it('announces the maximum the layout allows, and End reaches exactly it', () => {
    window.innerWidth = 1000; // 0.6 share caps this pane at 600, under maxWidth
    render(<Harness options={{ maxWidth: 900 }} />);

    expect(handle()).toHaveAttribute('aria-valuemax', '600');

    fireEvent.keyDown(handle(), { key: 'End' });

    expect(widthOf()).toBe('600px');
    expect(handle()).toHaveAttribute('aria-valuenow', '600');
  });

  it('falls back to the window share when the container has no layout yet', () => {
    render(<ContainerHarness containerWidth={0} siblingMinWidth={400} />);

    drag(400, 1400);

    expect(widthOf()).toBe('800px');
  });

  it('re-measures when another pane announces that it took more room', () => {
    let containerWidth = 900;
    function Sized() {
      const containerRef = useRef<HTMLDivElement | null>(null);
      const pane = useResizablePane({
        storageKey: STORAGE_KEY,
        defaultWidth: 300,
        minWidth: 200,
        maxWidth: 800,
        side: 'start',
        containerRef,
        siblingMinWidth: 400,
      });
      return (
        <div
          ref={(node) => {
            containerRef.current = node;
            if (node)
              Object.defineProperty(node, 'clientWidth', {
                configurable: true,
                get: () => containerWidth,
              });
          }}
        >
          <div data-testid="pane" style={{ width: pane.width }} />
          <PaneResizer pane={pane} label="Resize pane" />
        </div>
      );
    }

    render(<Sized />);
    drag(300, 800);
    expect(widthOf()).toBe('500px');

    // The task list widened: 200 less container, so this pane gives up 200 too.
    containerWidth = 700;
    act(() => announcePaneResize());

    expect(widthOf()).toBe('300px');
  });

  it('leaves the width alone for a non-primary button', () => {
    render(<Harness />);

    fireEvent.pointerDown(handle(), { button: 2, clientX: 300 });
    fireEvent.pointerMove(window, { clientX: 500 });

    expect(widthOf()).toBe('300px');
  });
});
