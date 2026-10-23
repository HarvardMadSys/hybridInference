'use client';

import { useEffect, useId, useState, type ReactNode } from 'react';

/**
 * Building blocks shared by the admin tabs, so a section title, a sub-tab bar,
 * a section index or a loading, empty or error state reads the same on every
 * tab. The look follows the Users, Providers and Recent Requests tabs.
 */

export interface AdminSubTab<K extends string> {
  key: K;
  label: string;
  /** Id of the tab button; the panel names it in `aria-labelledby`. */
  tabId: string;
  /** Id of the panel the tab controls. */
  panelId: string;
}

/** The underlined sub-tab bar the Providers tab uses, as an ARIA tablist. */
export function AdminSubTabs<K extends string>({
  label,
  tabs,
  active,
  onSelect,
}: {
  label: string;
  tabs: readonly AdminSubTab<K>[];
  active: K;
  onSelect?: (key: K) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label={label}
      className="flex flex-wrap items-center gap-1 border-b border-gray-200"
    >
      {tabs.map((tab) => {
        const isActive = tab.key === active;
        return (
          <button
            key={tab.key}
            type="button"
            id={tab.tabId}
            role="tab"
            aria-selected={isActive}
            aria-controls={tab.panelId}
            onClick={onSelect ? () => onSelect(tab.key) : undefined}
            className={`-mb-px border-b-2 px-3.5 py-1.5 text-[13px] font-medium transition ${
              isActive
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-900'
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * A titled section of a tab. It draws no box of its own: the title, its
 * description and `actions` (a primary button, a count) sit on the page, and
 * the content brings a panel only where a list or table needs one. The
 * heading names the region for assistive technology.
 */
export function AdminSection({
  id,
  title,
  description,
  badge,
  actions,
  className = '',
  children,
}: {
  /** Anchor for a section index. */
  id?: string;
  title: ReactNode;
  description?: ReactNode;
  /** Shown beside the title, e.g. a count of muted items. */
  badge?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  const headingId = `${useId()}-title`;
  return (
    <section id={id} aria-labelledby={headingId} className={`scroll-mt-4 ${className}`}>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={headingId} className="text-[14px] font-semibold text-gray-900">
              {title}
            </h2>
            {badge}
          </div>
          {description ? (
            <p className="mt-0.5 max-w-3xl text-[12px] leading-relaxed text-gray-500">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export interface AdminSectionNavItem {
  /** Id of the section element the item links to. */
  id: string;
  label: string;
  /** Small trailing note, e.g. how many settings in it are missing. */
  meta?: ReactNode;
}

/**
 * An index of a long tab's sections. On wide screens it is a column that stays
 * in view while the sections scroll past and marks the one being read; on
 * narrow ones it is a row of links that scrolls sideways above them. Each
 * item is an ordinary in-page link, so nothing is hidden from search or keys.
 */
export function AdminSectionNav({
  label,
  items,
}: {
  label: string;
  items: readonly AdminSectionNavItem[];
}) {
  const [marked, setMarked] = useState<string | null>(null);
  const idKey = items.map((item) => item.id).join('\n');
  // A filter can remove the marked section: fall back to the first one still
  // listed, so some item is always marked.
  const active = items.some((item) => item.id === marked) ? marked : (items[0]?.id ?? null);

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined' || !idKey) return;
    const elements = idKey
      .split('\n')
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    const inView = new Set<string>();
    // A section counts as being read while it crosses the top third of the
    // window; the first such section in page order is the current one.
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) inView.add(entry.target.id);
          else inView.delete(entry.target.id);
        }
        const current = elements.find((el) => inView.has(el.id));
        if (current) setMarked(current.id);
      },
      { rootMargin: '0px 0px -66% 0px' },
    );
    elements.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [idKey]);

  return (
    <nav
      aria-label={label}
      className="mb-6 lg:sticky lg:top-4 lg:mb-0 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto"
    >
      <ul className="-mx-1 flex gap-1 overflow-x-auto px-1 py-1 [scrollbar-width:thin] lg:flex-col lg:gap-0.5 lg:overflow-visible">
        {items.map((item) => {
          const isActive = item.id === active;
          return (
            <li key={item.id} className="shrink-0">
              <a
                href={`#${item.id}`}
                aria-current={isActive ? 'location' : undefined}
                onClick={() => setMarked(item.id)}
                className={`flex items-center justify-between gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-[13px] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 ${
                  isActive
                    ? 'bg-gray-100 font-medium text-gray-900'
                    : 'text-gray-500 hover:bg-gray-50 hover:text-gray-900'
                }`}
              >
                <span className="truncate">{item.label}</span>
                {item.meta}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** The spinning ring used across the admin while something loads. */
export function AdminSpinner({ size = 'md' }: { size?: 'sm' | 'md' }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block shrink-0 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900 ${
        size === 'sm' ? 'h-4 w-4' : 'h-5 w-5'
      }`}
    />
  );
}

/** A block-level loading state: spinner plus a short label. */
export function AdminLoading({
  label = 'Loading...',
  size = 'md',
}: {
  label?: string;
  /** `sm` inside a section, `md` where a whole list or tab loads. */
  size?: 'sm' | 'md';
}) {
  return (
    <div
      role="status"
      className={`flex flex-col items-center justify-center gap-2 ${size === 'sm' ? 'py-8' : 'py-16'}`}
    >
      <AdminSpinner />
      <p className="text-[12px] text-gray-400">{label}</p>
    </div>
  );
}

/**
 * Nothing to show. `inset` draws the dashed outline where the list would be;
 * `plain` is for a panel whose body is the list itself.
 */
export function AdminEmptyState({
  title,
  children,
  variant = 'inset',
}: {
  title: ReactNode;
  children?: ReactNode;
  variant?: 'inset' | 'plain';
}) {
  return (
    <div
      className={
        variant === 'inset'
          ? 'rounded-lg border border-dashed border-gray-200 px-4 py-6 text-center'
          : 'px-4 py-16 text-center'
      }
    >
      <p className="text-[13px] text-gray-500">{title}</p>
      {children ? <div className="mt-1 text-[12px] text-gray-400">{children}</div> : null}
    </div>
  );
}

const NOTICE_TONES = {
  error: 'border-red-200 bg-red-50 text-red-700',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  success: 'border-emerald-200 bg-emerald-50 text-emerald-700',
} as const;

/**
 * An inline message in a section: a failed load or save, with an optional action
 * (Retry) and an optional dismiss button.
 */
export function AdminNotice({
  tone = 'error',
  role,
  action,
  onDismiss,
  children,
}: {
  tone?: keyof typeof NOTICE_TONES;
  /** Defaults to none, so a message that was quiet before stays quiet. */
  role?: 'alert' | 'status';
  action?: ReactNode;
  onDismiss?: () => void;
  children: ReactNode;
}) {
  return (
    <div
      role={role}
      className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-[12px] ${NOTICE_TONES[tone]}`}
    >
      <div className="min-w-0 flex-1 break-words">{children}</div>
      {action}
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="-my-0.5 rounded px-1 text-[15px] font-semibold leading-none opacity-70 transition hover:opacity-100"
        >
          &times;
        </button>
      ) : null}
    </div>
  );
}
