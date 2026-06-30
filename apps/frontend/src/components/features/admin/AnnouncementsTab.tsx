'use client';

import { useEffect, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { BroadcastsTab } from '@/components/features/admin/BroadcastsTab';
import { SiteUpdatesTab } from '@/components/features/admin/SiteUpdatesTab';

export type AnnouncementsSubtab = 'updates' | 'email';

const UPDATES_TAB_ID = 'admin-announcements-updates-tab';
const UPDATES_PANEL_ID = 'admin-announcements-updates-panel';
const EMAIL_TAB_ID = 'admin-announcements-email-tab';
const EMAIL_PANEL_ID = 'admin-announcements-email-panel';

interface AnnouncementsTabProps {
  initialSubtab?: AnnouncementsSubtab;
}

export function AnnouncementsTab({ initialSubtab }: AnnouncementsTabProps = {}) {
  const router = useRouter();
  const pathname = usePathname();
  const [activeSubtab, setActiveSubtab] = useState<AnnouncementsSubtab>(initialSubtab ?? 'updates');

  useEffect(() => {
    setActiveSubtab(initialSubtab ?? 'updates');
  }, [initialSubtab]);

  const onSelectSubtab = (subtab: AnnouncementsSubtab) => {
    setActiveSubtab(subtab);
    router.replace(subtab === 'updates' ? pathname : `${pathname}?tab=${subtab}`, {
      scroll: false,
    });
  };

  const subtabClass = (active: boolean) =>
    `rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
      active ? 'bg-gray-900 text-white' : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
    }`;

  return (
    <div className="mt-5 space-y-6">
      <div
        className="flex flex-wrap items-center gap-1 border-b border-gray-200 pb-2"
        role="tablist"
      >
        <button
          type="button"
          id={UPDATES_TAB_ID}
          role="tab"
          aria-selected={activeSubtab === 'updates'}
          aria-controls={UPDATES_PANEL_ID}
          onClick={() => onSelectSubtab('updates')}
          className={subtabClass(activeSubtab === 'updates')}
        >
          Updates
        </button>
        <button
          type="button"
          id={EMAIL_TAB_ID}
          role="tab"
          aria-selected={activeSubtab === 'email'}
          aria-controls={EMAIL_PANEL_ID}
          onClick={() => onSelectSubtab('email')}
          className={subtabClass(activeSubtab === 'email')}
        >
          Email
        </button>
      </div>

      {activeSubtab === 'email' ? (
        <div id={EMAIL_PANEL_ID} role="tabpanel" aria-labelledby={EMAIL_TAB_ID}>
          <BroadcastsTab />
        </div>
      ) : (
        <div id={UPDATES_PANEL_ID} role="tabpanel" aria-labelledby={UPDATES_TAB_ID}>
          <SiteUpdatesTab />
        </div>
      )}
    </div>
  );
}
