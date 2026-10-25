'use client';

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';

import { popoverContainer, useAnchoredPopover } from './anchoredPopover';

export interface SelectOption<V extends string = string> {
  value: V;
  label: string;
  /** A second, muted line under the label. */
  description?: string;
  disabled?: boolean;
  /** Extra words the search matches, beyond the label, value and description. */
  keywords?: string;
}

export interface SelectOptionGroup<V extends string = string> {
  label: string;
  options: ReadonlyArray<SelectOption<V>>;
}

export type SelectItem<V extends string = string> = SelectOption<V> | SelectOptionGroup<V>;

export interface SelectProps<V extends string = string> {
  value: V;
  /**
   * Called once, on a deliberate choice of a *different* value: never while
   * the list is only opened, browsed or dismissed, and not when the current
   * option is chosen again (as a native `<select>` fires no change then).
   */
  onValueChange: (value: V) => void;
  options: ReadonlyArray<SelectItem<V>>;
  id?: string;
  'aria-label'?: string;
  'aria-labelledby'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
  'aria-required'?: boolean;
  /** Shown, muted, while the value matches no option. */
  placeholder?: string;
  disabled?: boolean;
  /** A search field in the list. By default, for lists of more than 10 options. */
  searchable?: boolean;
  searchPlaceholder?: string;
  /** What the search shows when nothing matches. */
  emptyText?: string;
  /**
   * `sm` for dense table rows; `md` matches the console's toolbar controls;
   * `lg` matches the taller text inputs of the routing and key forms.
   */
  size?: 'sm' | 'md' | 'lg';
  /** `dark` for pages that are always dark (the Playground). */
  tone?: 'light' | 'dark';
  /** Width and layout classes for the trigger, e.g. `w-full` or `w-40`. */
  className?: string;
  title?: string;
}

const SEARCH_THRESHOLD = 10;
const TYPEAHEAD_RESET_MS = 600;

function isGroup<V extends string>(item: SelectItem<V>): item is SelectOptionGroup<V> {
  return 'options' in item;
}

function matches<V extends string>(option: SelectOption<V>, terms: string[]): boolean {
  if (terms.length === 0) return true;
  const haystack = [option.label, option.value, option.description ?? '', option.keywords ?? '']
    .join(' ')
    .toLocaleLowerCase();
  return terms.every((term) => haystack.includes(term));
}

const TONES = {
  light: {
    trigger:
      'border-gray-200 bg-white text-gray-900 hover:border-gray-300 focus-visible:border-gray-400 focus-visible:ring-gray-400/30 disabled:hover:border-gray-200',
    triggerOpen: 'border-gray-400 ring-2 ring-gray-400/30',
    placeholder: 'text-gray-400',
    chevron: 'text-gray-400',
    panel: 'border-gray-200 bg-white shadow-lg',
    search:
      'border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-gray-400 focus:ring-gray-400/30',
    divider: 'border-gray-100',
    group: 'text-gray-400',
    option: 'text-gray-800',
    active: 'bg-gray-100',
    description: 'text-gray-500',
    check: 'text-gray-900',
    muted: 'text-gray-500',
  },
  dark: {
    trigger:
      'border-gray-700 bg-gray-800/70 text-gray-100 hover:border-gray-500 focus-visible:border-indigo-500 focus-visible:ring-indigo-500/30',
    triggerOpen: 'border-indigo-500 ring-2 ring-indigo-500/20',
    placeholder: 'text-gray-500',
    chevron: 'text-gray-400',
    panel: 'border-gray-700 bg-gray-900 shadow-2xl shadow-black/50',
    search:
      'border-gray-700 bg-gray-950/70 text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:ring-indigo-500/30',
    divider: 'border-gray-800',
    group: 'text-gray-500',
    option: 'text-gray-100',
    active: 'bg-indigo-500/15',
    description: 'text-gray-400',
    check: 'text-indigo-400',
    muted: 'text-gray-400',
  },
} as const;

/**
 * The frontend's one selection control: a select-only combobox with a
 * listbox popup, styled like the console and searchable for long lists.
 *
 * Keyboard: Enter, Space or an arrow opens it on the current option; arrows,
 * Home and End move (skipping disabled options); typing jumps to a match
 * (or, in a searchable list, filters it); Enter or Space chooses; Escape
 * closes without a change; Tab closes and moves on. Focus returns to the
 * trigger after a choice or Escape. Browsing never commits a value.
 */
export function Select<V extends string = string>({
  value,
  onValueChange,
  options,
  id,
  'aria-label': ariaLabel,
  'aria-labelledby': ariaLabelledBy,
  'aria-describedby': ariaDescribedBy,
  'aria-invalid': ariaInvalid,
  'aria-required': ariaRequired,
  placeholder,
  disabled = false,
  searchable,
  searchPlaceholder = 'Search…',
  emptyText = 'No matches',
  size = 'md',
  tone = 'light',
  className = '',
  title,
}: SelectProps<V>) {
  const generatedId = useId();
  const triggerId = id ?? `select-${generatedId}`;
  const listId = `${triggerId}-listbox`;
  const optionId = (index: number) => `${listId}-option-${index}`;
  const t = TONES[tone];

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeValue, setActiveValue] = useState<V | null>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const [listLabel, setListLabel] = useState<string | undefined>(ariaLabel);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const scrollToActive = useRef(true);
  const typeahead = useRef({ text: '', at: 0 });

  const allOptions = useMemo(
    () => options.flatMap((item) => (isGroup(item) ? item.options : [item])),
    [options],
  );
  // Decided when the list opens and kept until it closes: options arriving or
  // going away while it is open must not remove the search field that has
  // focus (or add one).
  const [openedSearchable, setOpenedSearchable] = useState(false);
  const isSearchable = open
    ? openedSearchable
    : (searchable ?? allOptions.length > SEARCH_THRESHOLD);
  const expanded = open && !disabled;
  const selected = allOptions.find((option) => option.value === value);

  // What the list shows: groups kept, filtered by the search.
  const terms = useMemo(
    () => query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean),
    [query],
  );
  const visibleItems = useMemo(() => {
    const result: Array<{ group?: string; options: SelectOption<V>[] }> = [];
    for (const item of options) {
      if (isGroup(item)) {
        const kept = item.options.filter((option) => matches(option, terms));
        if (kept.length) result.push({ group: item.label, options: kept });
      } else if (matches(item, terms)) {
        const last = result[result.length - 1];
        if (last && last.group === undefined) last.options.push(item);
        else result.push({ options: [item] });
      }
    }
    return result;
  }, [options, terms]);
  const visibleOptions = useMemo(() => visibleItems.flatMap((g) => g.options), [visibleItems]);
  const enabled = useMemo(() => visibleOptions.filter((o) => !o.disabled), [visibleOptions]);

  // The highlighted option, tracked by value so it survives options changing
  // while the list is open; if it goes away, fall back to the selected one or
  // the first that can be chosen.
  const active =
    enabled.find((o) => o.value === activeValue) ??
    enabled.find((o) => o.value === value) ??
    enabled[0];
  const activeIndex = active ? visibleOptions.indexOf(active) : -1;

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    setQuery('');
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const show = (start?: 'first' | 'last' | V) => {
    if (disabled) return;
    const pool = allOptions.filter((o) => !o.disabled);
    const target =
      start === 'first'
        ? pool[0]
        : start === 'last'
          ? pool[pool.length - 1]
          : (pool.find((o) => o.value === (start ?? value)) ?? pool[0]);
    setActiveValue(target?.value ?? null);
    scrollToActive.current = true;
    setQuery('');
    setOpenedSearchable(searchable ?? allOptions.length > SEARCH_THRESHOLD);
    setContainer(popoverContainer(triggerRef.current));
    if (!ariaLabel) {
      const label =
        (ariaLabelledBy && document.getElementById(ariaLabelledBy)?.textContent?.trim()) ||
        triggerRef.current?.labels?.[0]?.textContent?.trim();
      setListLabel(label || placeholder);
    }
    setOpen(true);
  };

  const choose = (option: SelectOption<V> | undefined) => {
    if (!option || option.disabled || disabled) return;
    close(true);
    if (option.value !== value) onValueChange(option.value);
  };

  const move = (to: 'next' | 'prev' | 'first' | 'last') => {
    if (enabled.length === 0) return;
    const at = active ? enabled.indexOf(active) : -1;
    const index =
      to === 'first'
        ? 0
        : to === 'last'
          ? enabled.length - 1
          : to === 'next'
            ? Math.min(enabled.length - 1, at + 1)
            : Math.max(0, at - 1);
    scrollToActive.current = true;
    setActiveValue(enabled[index].value);
  };

  /** Typing on a list without a search field: jump to the next label match. */
  const typeTo = (key: string) => {
    const now = Date.now();
    const state = typeahead.current;
    state.text = now - state.at > TYPEAHEAD_RESET_MS ? key : state.text + key;
    state.at = now;
    const text = state.text.toLocaleLowerCase();
    const pool = allOptions.filter((o) => !o.disabled);
    const from = active ? pool.indexOf(active) : -1;
    // A repeated single letter cycles through the options starting with it.
    const repeat = text.length > 1 && [...text].every((c) => c === text[0]);
    const needle = repeat ? text[0] : text;
    const ordered = [...pool.slice(from + 1), ...pool.slice(0, from + 1)];
    const startsAtCurrent = !repeat && text.length > 1 && active;
    const candidates = startsAtCurrent ? [active, ...ordered] : ordered;
    const hit = candidates.find((o) => o.label.toLocaleLowerCase().startsWith(needle));
    if (!hit) return;
    if (!expanded) show(hit.value);
    else {
      scrollToActive.current = true;
      setActiveValue(hit.value);
    }
  };

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  // Dismiss on a press or focus anywhere else, without taking focus back.
  useEffect(() => {
    if (!expanded) return;
    const outside = (event: Event) => {
      const target = event.target;
      if (
        target instanceof Node &&
        !triggerRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      ) {
        close(false);
      }
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('focusin', outside);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('focusin', outside);
    };
  }, [expanded, close]);

  const position = useAnchoredPopover(expanded, triggerRef, popoverRef);
  const placed = position.visibility !== 'hidden';

  // Move focus into the search field once per opening, after the popup is
  // placed: a browser will not focus it while it is still hidden, and later
  // re-placements (scroll, resize) must not pull focus back to it.
  const searchFocused = useRef(false);
  useEffect(() => {
    if (!expanded) {
      searchFocused.current = false;
      return;
    }
    if (!isSearchable || !placed || searchFocused.current) return;
    searchFocused.current = true;
    searchRef.current?.focus({ preventScroll: true });
  }, [expanded, isSearchable, placed]);

  // Keep the highlighted option in view by scrolling the list itself, never
  // the page or a dialog around it (as scrollIntoView could). Only when the
  // list opens or the highlight moves (and once the popup has its real size):
  // never in response to anything else, so a list the reader scrolled by
  // hand is left where they put it.
  const revealPending = useRef(false);
  const revealedFor = useRef<{ expanded: boolean; index: number }>({
    expanded: false,
    index: -1,
  });
  useLayoutEffect(() => {
    const last = revealedFor.current;
    if (expanded !== last.expanded || activeIndex !== last.index) {
      revealedFor.current = { expanded, index: activeIndex };
      revealPending.current = expanded;
    }
    const list = listRef.current;
    if (!revealPending.current || !list || activeIndex < 0) return;
    if (position.visibility === 'hidden') return; // not placed yet
    revealPending.current = false;
    if (!scrollToActive.current) return;
    const option = document.getElementById(optionId(activeIndex));
    if (!option) return;
    const top = option.offsetTop;
    const bottom = top + option.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight)
      list.scrollTop = bottom - list.clientHeight;
    // optionId is derived from listId, stable for this instance.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, activeIndex, position]);

  /** Keys shared by the trigger and the search field while the list is open. */
  const onOpenKey = (event: KeyboardEvent<HTMLElement>, inSearch: boolean): boolean => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        move('next');
        return true;
      case 'ArrowUp':
        event.preventDefault();
        move('prev');
        return true;
      case 'Home':
      case 'End':
        if (inSearch) return false;
        event.preventDefault();
        move(event.key === 'Home' ? 'first' : 'last');
        return true;
      case 'PageDown':
      case 'PageUp':
        event.preventDefault();
        move(event.key === 'PageDown' ? 'last' : 'first');
        return true;
      case 'Enter':
        event.preventDefault();
        choose(active);
        return true;
      case 'Escape':
        // Consumed here: a dialog or page listening for Escape must not also
        // close because the list did.
        event.preventDefault();
        event.stopPropagation();
        close(true);
        return true;
      case 'Tab':
        // Back on the trigger, the browser's own Tab then moves onward from it.
        close(true);
        return true;
      default:
        return false;
    }
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled || event.altKey || event.ctrlKey || event.metaKey) {
      if (event.altKey && (event.key === 'ArrowDown' || event.key === 'ArrowUp') && !expanded) {
        event.preventDefault();
        show();
      }
      return;
    }
    if (!expanded) {
      if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        show();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        show(selected && !selected.disabled ? selected.value : 'last');
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        show(event.key === 'Home' ? 'first' : 'last');
      } else if (event.key.length === 1 && event.key !== ' ' && !isSearchable) {
        event.preventDefault();
        typeTo(event.key);
      }
      return;
    }
    if (event.key === ' ' && !isSearchable) {
      event.preventDefault();
      choose(active);
      return;
    }
    if (onOpenKey(event, false)) return;
    if (event.key.length === 1 && !isSearchable) {
      event.preventDefault();
      typeTo(event.key);
    }
  };

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    onOpenKey(event, true);
  };

  const triggerLabel = selected?.label ?? (value ? String(value) : (placeholder ?? ''));
  const showingPlaceholder = !selected && !value;
  const sizeClasses =
    size === 'sm'
      ? 'h-7 gap-1.5 px-2 text-[12px]'
      : size === 'lg'
        ? 'h-9 gap-2 px-3 text-[13px]'
        : 'h-8 gap-2 px-2.5 text-[13px]';

  let optionIndex = -1;
  const renderOption = (option: SelectOption<V>) => {
    optionIndex += 1;
    const index = optionIndex;
    const isSelected = option.value === value && selected === option;
    const isActive = option === active;
    return (
      <div
        key={`${option.value}-${index}`}
        id={optionId(index)}
        role="option"
        data-value={option.value}
        aria-selected={isSelected}
        aria-disabled={option.disabled || undefined}
        onPointerMove={() => {
          if (option.disabled || option === active) return;
          scrollToActive.current = false;
          setActiveValue(option.value);
        }}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => choose(option)}
        className={`flex items-start gap-2 rounded-md px-2 py-1.5 text-[13px] leading-5 transition-colors duration-100 ${
          option.disabled
            ? `cursor-not-allowed opacity-50 ${t.option}`
            : `cursor-pointer ${t.option}`
        } ${isActive && !option.disabled ? t.active : ''}`}
      >
        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
          <span className={isSelected ? 'font-medium' : undefined}>{option.label}</span>
          {option.description ? (
            <span className={`block text-[12px] leading-4 ${t.description}`}>
              {option.description}
            </span>
          ) : null}
        </span>
        <svg
          aria-hidden="true"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          className={`mt-0.5 h-4 w-4 shrink-0 ${isSelected ? t.check : 'invisible'}`}
        >
          <path d="m4.5 10.5 3.5 3.5 7.5-8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
    );
  };

  const list: ReactNode = visibleItems.map((block, blockIndex) => {
    if (block.group === undefined) {
      return <div key={`flat-${blockIndex}`}>{block.options.map(renderOption)}</div>;
    }
    const groupId = `${listId}-group-${blockIndex}`;
    return (
      <div key={`group-${blockIndex}`} role="group" aria-labelledby={groupId}>
        <div
          id={groupId}
          className={`px-2 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide ${t.group}`}
        >
          {block.group}
        </div>
        {block.options.map(renderOption)}
      </div>
    );
  });

  return (
    <>
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        role="combobox"
        data-value={value}
        aria-haspopup="listbox"
        aria-expanded={expanded}
        aria-controls={expanded ? listId : undefined}
        aria-activedescendant={
          expanded && !isSearchable && activeIndex >= 0 ? optionId(activeIndex) : undefined
        }
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-describedby={ariaDescribedBy}
        aria-invalid={ariaInvalid || undefined}
        aria-required={ariaRequired || undefined}
        disabled={disabled}
        title={title}
        onClick={() => (expanded ? close(false) : show())}
        onKeyDown={onTriggerKeyDown}
        className={`inline-flex min-w-0 items-center rounded-md border text-left transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-60 ${sizeClasses} ${t.trigger} ${
          expanded ? t.triggerOpen : ''
        } ${ariaInvalid ? 'border-red-300' : ''} ${className}`}
      >
        <span className={`min-w-0 flex-1 truncate ${showingPlaceholder ? t.placeholder : ''}`}>
          {triggerLabel}
        </span>
        <svg
          aria-hidden="true"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          className={`h-3.5 w-3.5 shrink-0 transition-transform duration-150 motion-reduce:transition-none ${t.chevron} ${
            expanded ? 'rotate-180' : ''
          }`}
        >
          <path d="m5.5 8 4.5 4.5L14.5 8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {expanded && container
        ? createPortal(
            <div
              ref={popoverRef}
              style={position}
              data-select-popover=""
              className={`animate-admin-reveal z-50 flex flex-col overflow-hidden rounded-lg border ${t.panel}`}
            >
              {isSearchable ? (
                <div className={`shrink-0 border-b p-1.5 ${t.divider}`}>
                  <input
                    ref={searchRef}
                    type="text"
                    role="combobox"
                    aria-label={`Search ${listLabel ?? 'options'}`}
                    aria-autocomplete="list"
                    aria-expanded="true"
                    aria-controls={listId}
                    aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
                    value={query}
                    onChange={(event) => {
                      scrollToActive.current = true;
                      setQuery(event.target.value);
                      setActiveValue(null);
                    }}
                    onKeyDown={onSearchKeyDown}
                    placeholder={searchPlaceholder}
                    autoComplete="off"
                    spellCheck={false}
                    className={`h-8 w-full rounded-md border px-2.5 text-[13px] focus:outline-none focus:ring-2 ${t.search}`}
                  />
                </div>
              ) : null}
              <div
                ref={listRef}
                id={listId}
                role="listbox"
                aria-label={listLabel ? `${listLabel} options` : 'Options'}
                className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain p-1"
              >
                {list}
              </div>
              {visibleOptions.length === 0 ? (
                <p role="status" className={`px-3 py-4 text-center text-[12px] ${t.muted}`}>
                  {terms.length ? emptyText : 'No options'}
                </p>
              ) : isSearchable && terms.length ? (
                <p
                  role="status"
                  className={`shrink-0 border-t px-3 py-1.5 text-[11px] ${t.divider} ${t.muted}`}
                >
                  {visibleOptions.length} of {allOptions.length}
                </p>
              ) : null}
            </div>,
            container,
          )
        : null}
    </>
  );
}
