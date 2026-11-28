'use client';

import { AdminSection } from '@/components/features/admin/AdminPrimitives';
import { BUTTON_SECONDARY, PANEL, PILL } from '@/components/features/admin/adminStyles';

import type { OverviewItem, OverviewTone } from './settingsOverview';
import type { SettingsTabKey } from './settingsLayout';

const TONE_PILL: Readonly<Record<OverviewTone, string>> = {
  ok: 'bg-emerald-50 text-emerald-700',
  attention: 'bg-amber-50 text-amber-800',
  off: 'bg-gray-100 text-gray-600',
};

const TONE_DOT: Readonly<Record<OverviewTone, string>> = {
  ok: 'bg-emerald-500',
  attention: 'bg-amber-500',
  off: 'bg-gray-300',
};

/** A tab the Overview points to, with what it is for. */
export interface OverviewShortcut {
  key: SettingsTabKey;
  label: string;
  description: string;
}

/**
 * Admin → Settings → Overview: how the things set up once stand — the site's
 * address, email, sign-up, API access and alerts — each with the tab that
 * changes it, then every other tab with what it holds. It changes nothing
 * itself.
 */
export function SettingsOverview({
  items,
  shortcuts,
  onOpen,
}: {
  items: readonly OverviewItem[];
  /** The other tabs the page lists, in its order. */
  shortcuts: readonly OverviewShortcut[];
  /** Open a tab, at the section `hash` names when there is one. */
  onOpen: (key: SettingsTabKey, hash?: string) => void;
}) {
  const labelOf = (key: SettingsTabKey) => shortcuts.find((tab) => tab.key === key)?.label;
  const attention = items.filter((item) => item.tone === 'attention').length;
  return (
    <div className="space-y-8">
      {items.length > 0 ? (
        <AdminSection
          id="settings-status"
          title="Setup status"
          description={
            attention === 0
              ? 'Nothing here needs a look.'
              : `${attention} ${attention === 1 ? 'thing needs' : 'things need'} a look.`
          }
        >
          <ul className={`${PANEL} divide-y divide-gray-100`}>
            {items.map((item) => {
              const tab = labelOf(item.tab);
              return (
                <li
                  key={item.id}
                  data-overview-item={item.id}
                  className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
                >
                  <span
                    aria-hidden="true"
                    className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[item.tone]}`}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-2 text-[13px] font-medium text-gray-900">
                      {item.title}
                      <span className={`${PILL} ${TONE_PILL[item.tone]}`}>{item.status}</span>
                    </p>
                    <p className="mt-0.5 text-[12px] text-gray-500">{item.detail}</p>
                  </div>
                  {/* A tab the page does not list has nowhere to go. */}
                  {tab ? (
                    <button
                      type="button"
                      onClick={() => onOpen(item.tab, item.hash)}
                      className={BUTTON_SECONDARY}
                    >
                      Open {tab}
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </AdminSection>
      ) : null}

      <AdminSection
        id="settings-all-tabs"
        title="All settings"
        description="Every tab of this page, with what it holds."
      >
        <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {shortcuts.map((tab) => (
            <li key={tab.key}>
              <button
                type="button"
                onClick={() => onOpen(tab.key)}
                // A column from the top: a button centres what it holds.
                className={`${PANEL} flex h-full w-full flex-col items-start px-4 py-3 text-left transition hover:border-gray-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400`}
              >
                <span className="block text-[13px] font-medium text-gray-900">{tab.label}</span>
                <span className="mt-0.5 block text-[12px] leading-relaxed text-gray-500">
                  {tab.description}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </AdminSection>
    </div>
  );
}
