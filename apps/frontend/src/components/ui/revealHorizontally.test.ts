// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { revealHorizontally } from './revealHorizontally';

describe('revealHorizontally', () => {
  function box(left: number, width: number) {
    return { left, right: left + width, width, top: 0, bottom: 20, height: 20 } as DOMRect;
  }

  function scrollerWith(scrollLeft: number) {
    const scroller = document.createElement('nav');
    Object.defineProperty(scroller, 'clientWidth', { value: 300 });
    scroller.scrollLeft = scrollLeft;
    scroller.getBoundingClientRect = () => box(0, 300);
    return scroller;
  }

  it('scrolls the row, not the page, until the tab is visible', () => {
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    const scroller = scrollerWith(0);
    const item = document.createElement('a');
    // 700px into the row, off the right edge of a 300px-wide scroller.
    item.getBoundingClientRect = () => box(700, 80);

    revealHorizontally(scroller, item);

    expect(scroller.scrollLeft).toBe(700 + 80 - 300 + 16);
    expect(scrollTo).not.toHaveBeenCalled();
    scrollTo.mockRestore();
  });

  it('scrolls back for a tab left of the visible part and leaves a visible one alone', () => {
    const scroller = scrollerWith(400);
    const item = document.createElement('a');
    // On screen at -350, i.e. 50px into the row.
    item.getBoundingClientRect = () => box(-350, 60);
    revealHorizontally(scroller, item);
    expect(scroller.scrollLeft).toBe(34);

    const visible = scrollerWith(100);
    item.getBoundingClientRect = () => box(20, 60);
    revealHorizontally(visible, item);
    expect(visible.scrollLeft).toBe(100);
  });
});
