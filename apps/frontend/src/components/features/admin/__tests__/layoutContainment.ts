/**
 * jsdom does no layout, so a test cannot measure overflow. This checks the
 * structure that prevents it instead: from `inner` up to (not including)
 * `container`, every CSS grid has a zero-minimum base track
 * (`grid-cols-[minmax(0,…)]` or `grid-cols-1`, at every width, not only at a
 * breakpoint) and every grid item has `min-w-0`. Without both, an implicit
 * `auto` track grows to fit a long word or a wide table and widens the page.
 */
export function uncontainedGrids(inner: Element, container: Element): string[] {
  const problems: string[] = [];
  for (let el = inner.parentElement; el && el !== container; el = el.parentElement) {
    const classes = el.className.split(/\s+/);
    if (!classes.includes('grid')) continue;
    if (!classes.some((c) => c.startsWith('grid-cols-[minmax(0,') || c === 'grid-cols-1')) {
      problems.push(`grid without a minmax(0,…) base track: ${el.className}`);
    }
    for (const item of Array.from(el.children)) {
      if (item.contains(inner) && !item.className.split(/\s+/).includes('min-w-0')) {
        problems.push(`grid item without min-w-0: ${item.className}`);
      }
    }
  }
  return problems;
}

const POSITIONED = ['relative', 'absolute', 'fixed', 'sticky'];

/**
 * `sr-only` text is absolutely positioned. Inside a horizontal scroller that
 * is not itself positioned, its containing block is further out, so the
 * scroller does not clip it: at the far end of a wide table or a sideways
 * row of links it widens the whole page. Lists every such node under `root`.
 */
export function escapingScreenReaderText(root: Element): string[] {
  const problems: string[] = [];
  for (const node of Array.from(root.querySelectorAll('.sr-only'))) {
    for (let el = node.parentElement; el && el !== root.parentElement; el = el.parentElement) {
      const classes = el.className.split(/\s+/);
      if (classes.some((c) => POSITIONED.includes(c))) break;
      if (classes.includes('overflow-x-auto')) {
        problems.push(
          `"${node.textContent}" escapes ${el.tagName.toLowerCase()}.${classes.join('.')}`,
        );
        break;
      }
    }
  }
  return problems;
}
