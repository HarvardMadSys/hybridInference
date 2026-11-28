'use client';

import { Fragment, useEffect, useRef, type ReactNode } from 'react';

import { revealHorizontally } from '@/components/ui/revealHorizontally';

export interface SettingsNavTab<K extends string> {
  key: K;
  label: string;
  /** The heading the tab is listed under; a tab without one leads the list. */
  group?: string;
  /** Id of the tab button; the panel names it in `aria-labelledby`. */
  tabId: string;
  /** Id of the panel the tab controls. */
  panelId: string;
  /** Small trailing note for the eye (unsaved changes, settings missing), hidden from assistive technology. */
  meta?: ReactNode;
  /**
   * What `meta` says, as words that follow the tab's name: ", unsaved changes,
   * 1 missing". The tab is then named in full: as a flex item its label would
   * be read with a space before the comma.
   */
  status?: string;
}

/**
 * The tabs of Admin → Settings, as one ARIA tablist. On a wide screen it is a
 * column beside the tab on screen, under the headings of its groups; on a
 * narrow one, a row that scrolls sideways and keeps the chosen tab in view,
 * with the headings left out. The headings are hidden from assistive
 * technology, whose tablist holds only tabs: each tab's name says what it holds.
 */
export function SettingsNav<K extends string>({
  label,
  tabs,
  active,
  onSelect,
}: {
  label: string;
  tabs: readonly SettingsNavTab<K>[];
  active: K;
  onSelect: (key: K) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Only the row scrolls sideways: as a column the list is as wide as it is.
  useEffect(() => {
    const scroller = ref.current;
    const current = scroller?.querySelector<HTMLElement>('[aria-selected="true"]');
    if (scroller && current && scroller.scrollWidth > scroller.clientWidth) {
      revealHorizontally(scroller, current);
    }
  }, [active]);

  return (
    // The padding keeps focus rings inside the scroller.
    <div
      ref={ref}
      role="tablist"
      aria-label={label}
      className="-mx-1 flex gap-1 overflow-x-auto px-1 py-1 [scrollbar-width:thin] lg:mx-0 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:p-0"
    >
      {tabs.map((tab, index) => {
        const heading = tab.group && tab.group !== tabs[index - 1]?.group ? tab.group : null;
        const isActive = tab.key === active;
        return (
          <Fragment key={tab.key}>
            {heading ? (
              <p
                aria-hidden="true"
                className="hidden px-3 pb-1 pt-4 text-[11px] font-medium uppercase tracking-wide text-gray-400 lg:block"
              >
                {heading}
              </p>
            ) : null}
            <button
              type="button"
              id={tab.tabId}
              role="tab"
              aria-selected={isActive}
              aria-controls={tab.panelId}
              aria-label={tab.status ? `${tab.label}${tab.status}` : undefined}
              onClick={() => onSelect(tab.key)}
              className={`flex shrink-0 items-center justify-between gap-2 whitespace-nowrap rounded-md px-3 py-1.5 text-left text-[13px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 ${
                isActive
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
              }`}
            >
              <span className="truncate">{tab.label}</span>
              {tab.meta ? <span aria-hidden="true">{tab.meta}</span> : null}
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}
