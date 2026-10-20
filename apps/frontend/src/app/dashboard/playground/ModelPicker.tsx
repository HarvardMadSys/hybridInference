'use client';

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';

export interface PlaygroundModel {
  id: string;
  name: string;
  provider: string;
  providers: { id: string; name: string }[];
}

interface ModelPickerProps {
  id?: string;
  models: PlaygroundModel[];
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
  loading?: boolean;
  /** Shown instead of "No models available" when the list is empty for a known reason. */
  emptyLabel?: string;
}

function modelLabel(name: string): { name: string; detail?: string } {
  const match = name.match(/^(.*?)\s*(\([^()]*\))$/);
  return match && match[1] ? { name: match[1].trimEnd(), detail: match[2] } : { name };
}

export function ModelPicker({
  id,
  models,
  value,
  onChange,
  disabled = false,
  loading = false,
  emptyLabel = 'No models available',
}: ModelPickerProps) {
  const generatedId = useId();
  const triggerId = id ?? `model-picker-${generatedId}`;
  const listId = `${triggerId}-list`;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState<string | null>(null);
  const [position, setPosition] = useState<CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef(new Map<string, HTMLDivElement>());
  const scrollActiveRef = useRef(true);
  const unavailable = disabled || loading || models.length === 0;
  const isOpen = open && !unavailable;
  const selected = models.find((model) => model.id === value);
  const selectedLabel = selected ? modelLabel(selected.name) : null;
  const placeholder = loading
    ? 'Loading models…'
    : models.length === 0
      ? emptyLabel
      : 'Select a model';

  const filtered = useMemo(() => {
    const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return models.filter((model) => {
      const searchable = [
        model.name,
        model.id,
        model.provider,
        ...model.providers.flatMap((provider) => [provider.name, provider.id]),
      ]
        .join(' ')
        .toLocaleLowerCase();
      return terms.every((term) => searchable.includes(term));
    });
  }, [models, query]);
  const activeIndex = Math.max(
    0,
    filtered.findIndex((model) => model.id === activeId),
  );
  const activeModel = filtered[activeIndex];

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const show = (last = false) => {
    if (unavailable) return;
    scrollActiveRef.current = true;
    setQuery('');
    setActiveId(selected?.id ?? (last ? models[models.length - 1]?.id : models[0]?.id) ?? null);
    setOpen(true);
  };

  const choose = (modelId: string) => {
    if (unavailable) return;
    if (modelId !== value) onChange(modelId);
    close(true);
  };

  useEffect(() => {
    if (unavailable) setOpen(false);
  }, [unavailable]);

  useLayoutEffect(() => {
    if (!isOpen) return;
    const reposition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const viewport = window.visualViewport;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const margin = 8;
      const gap = 6;
      const width = Math.min(400, viewportWidth - margin * 2);
      const below = Math.max(0, viewportTop + viewportHeight - rect.bottom - gap - margin);
      const above = Math.max(0, rect.top - viewportTop - gap - margin);
      const placeBelow = below >= 280 || below >= above;
      setPosition({
        left: Math.max(
          viewportLeft + margin,
          Math.min(rect.left, viewportLeft + viewportWidth - width - margin),
        ),
        width,
        maxHeight: Math.min(420, placeBelow ? below : above),
        ...(placeBelow
          ? { top: rect.bottom + gap }
          : { bottom: window.innerHeight - rect.top + gap }),
      });
    };
    reposition();
    searchRef.current?.focus({ preventScroll: true });
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    window.visualViewport?.addEventListener('resize', reposition);
    window.visualViewport?.addEventListener('scroll', reposition);
    return () => {
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
      window.visualViewport?.removeEventListener('resize', reposition);
      window.visualViewport?.removeEventListener('scroll', reposition);
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const dismissOutside = (event: Event) => {
      const target = event.target;
      if (
        target instanceof Node &&
        !popoverRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      ) {
        close();
      }
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('focusin', dismissOutside);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside);
      document.removeEventListener('focusin', dismissOutside);
    };
  }, [isOpen, close]);

  useEffect(() => {
    if (isOpen && activeModel && scrollActiveRef.current) {
      optionRefs.current.get(activeModel.id)?.scrollIntoView({ block: 'nearest' });
    }
  }, [isOpen, activeModel]);

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (filtered.length) {
        const direction = event.key === 'ArrowDown' ? 1 : -1;
        const index = (activeIndex + direction + filtered.length) % filtered.length;
        scrollActiveRef.current = true;
        setActiveId(filtered[index].id);
      }
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (activeModel) choose(activeModel.id);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'Tab') {
      // Resume the sidebar's natural tab order instead of the portal's document position.
      close(true);
    }
  };

  return (
    <>
      <button
        id={triggerId}
        ref={triggerRef}
        type="button"
        disabled={unavailable}
        aria-label={`Model: ${selected?.name ?? placeholder}`}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={isOpen ? listId : undefined}
        title={selected ? `${selected.name}\n${selected.id}` : placeholder}
        onClick={() => (isOpen ? close() : show())}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            show(event.key === 'ArrowUp');
          } else if (event.key === 'Escape' && isOpen) {
            close();
          }
        }}
        className={`flex w-full items-center gap-3 rounded-lg border bg-gray-800/70 px-3 py-2.5 text-left text-sm outline-none transition hover:border-gray-500 hover:bg-gray-800 focus-visible:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/30 disabled:cursor-not-allowed disabled:opacity-50 ${
          isOpen ? 'border-indigo-500 ring-2 ring-indigo-500/20' : 'border-gray-700'
        }`}
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium text-gray-100">
            {selectedLabel?.name ?? placeholder}
          </span>
          {selected && (
            <span className="mt-0.5 block truncate text-xs text-gray-400">{selected.id}</span>
          )}
        </span>
        <svg
          aria-hidden="true"
          className={`h-4 w-4 shrink-0 text-gray-400 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="m5 7.5 5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {isOpen &&
        createPortal(
          <div
            ref={popoverRef}
            style={position}
            className="fixed z-50 flex flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-900 text-white shadow-2xl shadow-black/50"
          >
            <div className="shrink-0 border-b border-gray-800 p-2.5">
              <div className="flex items-center gap-2 rounded-lg border border-gray-700 bg-gray-950/70 px-3 focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500/30">
                <svg
                  aria-hidden="true"
                  className="h-4 w-4 shrink-0 text-gray-500"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                >
                  <circle cx="8.5" cy="8.5" r="5.5" />
                  <path d="m12.5 12.5 4 4" strokeLinecap="round" />
                </svg>
                <input
                  ref={searchRef}
                  type="text"
                  role="combobox"
                  aria-label="Search models"
                  aria-autocomplete="list"
                  aria-expanded="true"
                  aria-controls={listId}
                  aria-activedescendant={activeModel ? `${listId}-${activeIndex}` : undefined}
                  value={query}
                  onChange={(event) => {
                    scrollActiveRef.current = true;
                    setQuery(event.target.value);
                    setActiveId(null);
                  }}
                  onKeyDown={onSearchKeyDown}
                  placeholder="Search models or providers…"
                  autoComplete="off"
                  spellCheck={false}
                  className="min-w-0 flex-1 bg-transparent py-2 text-sm text-gray-100 outline-none placeholder:text-gray-500"
                />
                <span aria-hidden="true" className="text-[10px] text-gray-500">
                  ESC
                </span>
              </div>
              <div aria-live="polite" className="px-1 pt-2 text-xs text-gray-400">
                {query.trim()
                  ? `${filtered.length} of ${models.length} models`
                  : `${models.length} ${models.length === 1 ? 'model' : 'models'}`}
              </div>
            </div>

            <div
              id={listId}
              role="listbox"
              aria-label="Models"
              className="dark-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5"
            >
              {filtered.map((model, index) => {
                const label = modelLabel(model.name);
                const isSelected = model.id === value;
                const isActive = model.id === activeModel?.id;
                return (
                  <div
                    key={model.id}
                    id={`${listId}-${index}`}
                    ref={(element) => {
                      if (element) optionRefs.current.set(model.id, element);
                      else optionRefs.current.delete(model.id);
                    }}
                    role="option"
                    aria-label={`${model.name}, ${model.id}`}
                    aria-selected={isSelected}
                    onPointerMove={() => {
                      scrollActiveRef.current = false;
                      setActiveId(model.id);
                    }}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => choose(model.id)}
                    className={`flex cursor-pointer items-start gap-3 rounded-lg px-3 py-2.5 outline-none transition-colors ${
                      isActive
                        ? 'bg-indigo-500/15 ring-1 ring-inset ring-indigo-400/30'
                        : isSelected
                          ? 'bg-gray-800/70'
                          : 'hover:bg-gray-800/70'
                    }`}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                        <span className="max-w-full break-words text-sm font-medium leading-5 text-gray-100">
                          {label.name}
                        </span>
                        {label.detail && (
                          <span className="max-w-full rounded bg-gray-800 px-1.5 py-0.5 text-[11px] leading-4 text-gray-400">
                            {label.detail}
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block break-all text-xs leading-4 text-gray-400">
                        {model.id}
                      </span>
                    </span>
                    {isSelected && (
                      <svg
                        aria-hidden="true"
                        className="mt-0.5 h-4 w-4 shrink-0 text-indigo-400"
                        viewBox="0 0 20 20"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                      >
                        <path d="m4 10 4 4 8-8" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </div>
                );
              })}
            </div>

            {filtered.length === 0 && (
              <div className="px-6 py-8 text-center">
                <p className="text-sm font-medium text-gray-200">No models found</p>
                <p className="mt-1 text-xs text-gray-400">
                  Try another model name, ID, or provider.
                </p>
              </div>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
