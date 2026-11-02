/**
 * Scroll `scroller` sideways just enough to show `item` whole. Only the
 * row's own `scrollLeft` moves: `scrollIntoView` would also scroll the page.
 */
export function revealHorizontally(scroller: HTMLElement, item: HTMLElement) {
  const box = scroller.getBoundingClientRect();
  const rect = item.getBoundingClientRect();
  // Positions within the row's scrollable content.
  const left = rect.left - box.left + scroller.scrollLeft;
  const right = left + rect.width;
  if (left < scroller.scrollLeft) {
    scroller.scrollLeft = Math.max(0, left - 16);
  } else if (right > scroller.scrollLeft + scroller.clientWidth) {
    scroller.scrollLeft = right - scroller.clientWidth + 16;
  }
}
