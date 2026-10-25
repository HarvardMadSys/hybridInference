import { fireEvent, screen, within } from '@testing-library/react';

/**
 * The open list of the shared `Select` whose trigger is `trigger`, opening
 * it first if needed. Found through the trigger's `aria-controls`, so another
 * list left open elsewhere is never mistaken for it.
 */
export function openOptions(trigger: HTMLElement): HTMLElement {
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger);
  const id = trigger.getAttribute('aria-controls');
  const listbox = id ? document.getElementById(id) : null;
  if (!listbox) throw new Error('The select did not open a list');
  return listbox;
}

/**
 * Chooses `option` in the shared `Select` labelled `name`, the way a person
 * does: open the combobox, then click the option in its list.
 */
export function chooseOption(name: string | RegExp, option: string | RegExp, root?: HTMLElement) {
  const scope = root ? within(root) : screen;
  const trigger = scope.getByRole('combobox', { name });
  fireEvent.click(within(openOptions(trigger)).getByRole('option', { name: option }));
  return trigger;
}

/**
 * Chooses the option whose value is `value` in the `Select` whose trigger is
 * `trigger`, through the real list: open it, click the option. For tests
 * whose fixtures name options by value (provider ids, key ids).
 */
export function chooseValue(trigger: HTMLElement, value: string) {
  const option = within(openOptions(trigger))
    .getAllByRole('option')
    .find((candidate) => candidate.getAttribute('data-value') === value);
  if (!option) throw new Error(`No option with value "${value}" in the open list`);
  fireEvent.click(option);
  return trigger;
}

/** The labels of the options in the open list, in order. */
export function optionLabels(): string[] {
  return within(screen.getByRole('listbox'))
    .getAllByRole('option')
    .map((option) => option.textContent ?? '');
}

/**
 * Makes jsdom refuse to focus an element inside something with inline
 * `visibility: hidden`, as a browser does (jsdom focuses anything). Returns
 * a function that restores the original `focus`.
 */
export function refuseFocusWhileHidden(): () => void {
  const original = HTMLElement.prototype.focus;
  const insideHidden = (el: HTMLElement | null): boolean =>
    el !== null && (el.style.visibility === 'hidden' || insideHidden(el.parentElement));
  HTMLElement.prototype.focus = function focus(this: HTMLElement, options?: FocusOptions) {
    if (insideHidden(this)) return;
    original.call(this, options);
  };
  return () => {
    HTMLElement.prototype.focus = original;
  };
}
