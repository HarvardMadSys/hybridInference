// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Select, type SelectItem } from './Select';
import { refuseFocusWhileHidden } from '@/test-utils/select';

afterEach(cleanup);

const FRUIT: SelectItem[] = [
  { value: 'apple', label: 'Apple' },
  { value: 'banana', label: 'Banana', disabled: true },
  { value: 'cherry', label: 'Cherry' },
  { value: 'date', label: 'Date' },
];

function Harness({
  options = FRUIT,
  initial = 'apple',
  onValueChange = vi.fn(),
  ...rest
}: {
  options?: SelectItem[];
  initial?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  searchable?: boolean;
  disabled?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div>
      <label htmlFor="fruit">Fruit</label>
      <Select
        id="fruit"
        value={value}
        options={options}
        onValueChange={(next) => {
          setValue(next);
          onValueChange(next);
        }}
        {...rest}
      />
      <input aria-label="After" />
    </div>
  );
}

const trigger = () => screen.getByRole('combobox', { name: 'Fruit' });
const highlighted = () => {
  const id = trigger().getAttribute('aria-activedescendant');
  return id ? document.getElementById(id)?.textContent : null;
};
const key = (target: HTMLElement, k: string, init: object = {}) =>
  fireEvent.keyDown(target, { key: k, ...init });

describe('Select', () => {
  it('is a labelled combobox that shows the current option and opens a listbox', () => {
    render(<Harness />);
    expect(trigger()).toHaveTextContent('Apple');
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    fireEvent.click(trigger());

    const list = screen.getByRole('listbox', { name: 'Fruit options' });
    expect(trigger()).toHaveAttribute('aria-expanded', 'true');
    expect(trigger()).toHaveAttribute('aria-controls', list.id);
    expect(within(list).getByRole('option', { name: 'Apple' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(within(list).getByRole('option', { name: 'Banana' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('browses with the keyboard without committing, skipping disabled options', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    act(() => trigger().focus());

    key(trigger(), 'ArrowDown');
    expect(highlighted()).toBe('Apple');
    key(trigger(), 'ArrowDown');
    expect(highlighted()).toBe('Cherry'); // Banana is disabled
    key(trigger(), 'End');
    expect(highlighted()).toBe('Date');
    key(trigger(), 'Home');
    expect(highlighted()).toBe('Apple');
    expect(onValueChange).not.toHaveBeenCalled();

    key(trigger(), 'ArrowDown');
    key(trigger(), 'Enter');
    expect(onValueChange).toHaveBeenCalledTimes(1);
    expect(onValueChange).toHaveBeenCalledWith('cherry');
    expect(trigger()).toHaveTextContent('Cherry');
    expect(trigger()).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('opens on Space and Enter, and Space chooses the highlighted option', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    key(trigger(), ' ');
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    key(trigger(), 'End');
    key(trigger(), ' ');
    expect(onValueChange).toHaveBeenCalledWith('date');

    key(trigger(), 'Enter');
    expect(screen.getByRole('listbox')).toBeInTheDocument();
  });

  it('does not report choosing the option that is already selected', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);

    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole('option', { name: 'Apple' }));
    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    key(trigger(), 'Enter');
    key(trigger(), 'Enter');
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('ignores clicks on a disabled option', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole('option', { name: 'Banana' }));
    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.getByRole('listbox')).toBeInTheDocument();
  });

  it('closes on Escape without a change, keeps focus, and keeps Escape to itself', () => {
    const onValueChange = vi.fn();
    const windowEscape = vi.fn();
    const onWindowKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') windowEscape();
    };
    window.addEventListener('keydown', onWindowKey);
    render(<Harness onValueChange={onValueChange} />);
    act(() => trigger().focus());
    key(trigger(), 'ArrowDown');
    key(trigger(), 'ArrowDown');

    key(trigger(), 'Escape');

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onValueChange).not.toHaveBeenCalled();
    expect(trigger()).toHaveFocus();
    expect(windowEscape).not.toHaveBeenCalled();
    window.removeEventListener('keydown', onWindowKey);
  });

  it('closes on Tab and on a press outside, without changing the value', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    key(trigger(), 'ArrowDown');
    key(trigger(), 'ArrowDown');
    key(trigger(), 'Tab');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    fireEvent.click(trigger());
    fireEvent.pointerDown(screen.getByLabelText('After'));
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('jumps to a typed label without committing it', () => {
    const onValueChange = vi.fn();
    render(<Harness onValueChange={onValueChange} />);
    act(() => trigger().focus());
    key(trigger(), 'd');
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(highlighted()).toBe('Date');
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('shows a placeholder for an empty value and an unknown value as it is', () => {
    const { unmount } = render(<Harness initial="" placeholder="Pick a fruit" />);
    expect(trigger()).toHaveTextContent('Pick a fruit');
    unmount();
    render(<Harness initial="mango" placeholder="Pick a fruit" />);
    // Not misreported as the first option, as a native select would.
    expect(trigger()).toHaveTextContent('mango');
  });

  it('labels groups quietly and keeps them in the list', () => {
    render(
      <Harness
        initial=""
        options={[
          { value: '', label: 'All' },
          { label: 'Citrus', options: [{ value: 'lemon', label: 'Lemon' }] },
        ]}
      />,
    );
    fireEvent.click(trigger());
    const group = screen.getByRole('group', { name: 'Citrus' });
    expect(within(group).getByRole('option', { name: 'Lemon' })).toBeInTheDocument();
  });

  it('searches a long list, says when nothing matches, and Enter takes the highlight', () => {
    const onValueChange = vi.fn();
    const many: SelectItem[] = Array.from({ length: 14 }, (_, i) => ({
      value: `model-${i}`,
      label: `Model ${i}`,
      keywords: i === 7 ? 'vision' : undefined,
    }));
    render(<Harness options={many} initial="model-0" onValueChange={onValueChange} />);
    fireEvent.click(trigger());
    const search = screen.getByRole('combobox', { name: 'Search Fruit' });
    expect(search).toHaveFocus();

    fireEvent.change(search, { target: { value: 'zzz' } });
    expect(screen.getByRole('status')).toHaveTextContent('No matches');

    fireEvent.change(search, { target: { value: 'vision' } });
    expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent('1 of 14');
    key(search, 'Enter');
    expect(onValueChange).toHaveBeenCalledWith('model-7');
    expect(trigger()).toHaveFocus();
  });

  it('keeps a sensible highlight when the options change while open', () => {
    function Live() {
      const [opts, setOpts] = useState<SelectItem[]>(FRUIT);
      return (
        <>
          <Harness options={opts} />
          <button type="button" onClick={() => setOpts([{ value: 'apple', label: 'Apple' }])}>
            shrink
          </button>
        </>
      );
    }
    render(<Live />);
    key(trigger(), 'ArrowDown');
    key(trigger(), 'End');
    expect(highlighted()).toBe('Date');

    // Fired directly so the open list is not dismissed by the press.
    act(() => screen.getByRole('button', { name: 'shrink' }).click());
    expect(highlighted()).toBe('Apple');
  });

  it('does nothing while disabled', () => {
    const onValueChange = vi.fn();
    render(<Harness disabled onValueChange={onValueChange} />);
    expect(trigger()).toBeDisabled();
    fireEvent.click(trigger());
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('renders its list inside the nearest dialog, so the dialog still contains it', () => {
    render(
      <div role="dialog" aria-label="Edit">
        <Harness />
      </div>,
    );
    fireEvent.click(trigger());
    expect(screen.getByRole('dialog', { name: 'Edit' })).toContainElement(
      screen.getByRole('listbox'),
    );
  });

  it('renders its list inside the console theme wrapper, so dark mode still applies', () => {
    render(
      <div className="console-theme" data-testid="theme">
        <Harness />
      </div>,
    );
    fireEvent.click(trigger());
    expect(screen.getByTestId('theme')).toContainElement(screen.getByRole('listbox'));
  });

  it('keeps its search field while open even if the options shrink, so keys keep working', () => {
    const many: SelectItem[] = Array.from({ length: 11 }, (_, i) => ({
      value: `m${i}`,
      label: `Model ${i}`,
    }));
    function Shrinking() {
      const [opts, setOpts] = useState<SelectItem[]>(many);
      return (
        <>
          <Harness options={opts} initial="m0" />
          <button type="button" onClick={() => setOpts(many.slice(0, 2))}>
            shrink
          </button>
        </>
      );
    }
    render(<Shrinking />);
    fireEvent.click(trigger());
    const search = screen.getByRole('combobox', { name: 'Search Fruit' });
    expect(search).toHaveFocus();

    act(() => screen.getByRole('button', { name: 'shrink' }).click());
    act(() => search.focus());

    expect(screen.getByRole('combobox', { name: 'Search Fruit' })).toBe(search);
    key(search, 'ArrowDown');
    expect(search.getAttribute('aria-activedescendant')).toBeTruthy();
    key(search, 'Escape');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
  });

  it('positions its popup out of the page flow from the first render and never scrolls the page', () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView as unknown as Element['scrollIntoView'];
    render(<Harness />);
    act(() => trigger().focus());
    key(trigger(), 'ArrowDown');

    const popup = screen.getByRole('listbox').parentElement!;
    expect(popup.style.position).toBe('fixed');
    key(trigger(), 'End');
    key(trigger(), 'Home');
    expect(scrollIntoView).not.toHaveBeenCalled();
    // @ts-expect-error jsdom has no scrollIntoView of its own
    delete Element.prototype.scrollIntoView;
  });

  it('leaves a list the reader scrolled by hand where they put it', () => {
    const many: SelectItem[] = Array.from({ length: 30 }, (_, i) => ({
      value: `v${i}`,
      label: `Option ${i}`,
    }));
    render(<Harness options={many} initial="v0" searchable={false} />);
    act(() => trigger().focus());
    key(trigger(), 'ArrowDown'); // opens with Option 0 highlighted

    // Give the list a layout: 30px rows in a 100px window, tracking writes.
    const list = screen.getByRole('listbox');
    let scrollTop = 0;
    const writes: number[] = [];
    Object.defineProperty(list, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => {
        writes.push(value);
        scrollTop = value;
      },
    });
    Object.defineProperty(list, 'clientHeight', { configurable: true, value: 100 });
    within(list)
      .getAllByRole('option')
      .forEach((option, index) => {
        Object.defineProperty(option, 'offsetTop', { configurable: true, value: index * 30 });
        Object.defineProperty(option, 'offsetHeight', { configurable: true, value: 30 });
      });

    // The wheel scrolls the highlighted option out of view; the list's own
    // scroll event must not snap it back.
    scrollTop = 450;
    fireEvent.scroll(list);
    fireEvent.scroll(window);
    expect(writes).toEqual([]);
    expect(scrollTop).toBe(450);

    // Moving the highlight with the keyboard still brings it into view.
    key(trigger(), 'ArrowDown');
    expect(writes).toEqual([30]);
  });

  it('focuses the search once the popup is placed, and only once per opening', () => {
    // As in a browser: nothing inside the still-hidden, unplaced popup can
    // take focus, so focusing it on the first render would silently fail.
    const restore = refuseFocusWhileHidden();
    try {
      const many: SelectItem[] = Array.from({ length: 14 }, (_, i) => ({
        value: `m${i}`,
        label: `Model ${i}`,
      }));
      render(<Harness options={many} initial="m0" />);

      fireEvent.click(trigger());
      const search = screen.getByRole('combobox', { name: 'Search Fruit' });
      expect(search.closest('[data-select-popover]')).not.toHaveStyle({ visibility: 'hidden' });
      expect(search).toHaveFocus();

      // A later re-placement (scroll, resize) does not pull focus back while
      // the list stays open.
      act(() => search.blur());
      fireEvent.scroll(window);
      fireEvent(window, new Event('resize'));
      expect(screen.getByRole('listbox')).toBeInTheDocument();
      expect(search).not.toHaveFocus();
    } finally {
      restore();
    }
  });
});
