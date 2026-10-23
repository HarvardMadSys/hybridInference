'use client';

import { useId, type ReactNode } from 'react';

import { CARD } from './adminStyles';

/**
 * Building blocks shared by the admin tabs, so a card title, a sub-tab bar or
 * a loading, empty or error state reads the same on every tab. The look
 * follows the Users, Providers and Recent Requests tabs.
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
 * A titled card: the heading names the region for assistive technology, the
 * description sits under it, and `actions` (refresh, counts) align right.
 */
export function AdminSection({
  title,
  description,
  badge,
  actions,
  className = '',
  bodyClassName = 'p-5',
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  /** Shown beside the title, e.g. a count of muted items. */
  badge?: ReactNode;
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}) {
  const headingId = `${useId()}-title`;
  return (
    <section aria-labelledby={headingId} className={`${CARD} ${className}`}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-gray-100 px-5 py-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={headingId} className="text-[15px] font-semibold text-gray-900">
              {title}
            </h2>
            {badge}
          </div>
          {description ? (
            <p className="mt-1 max-w-3xl text-[12px] leading-relaxed text-gray-500">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      <div className={bodyClassName}>{children}</div>
    </section>
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
  /** `sm` inside a card section, `md` where a whole list or tab loads. */
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
 * Nothing to show. `inset` draws the dashed outline for use inside a card
 * section; `plain` is for a card whose body is the list itself.
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
 * An inline message in a card: a failed load or save, with an optional action
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
