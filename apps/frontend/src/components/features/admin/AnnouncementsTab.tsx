'use client';

import { useEffect, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { AdminSubTabs } from '@/components/features/admin/AdminPrimitives';
import { BroadcastsTab } from '@/components/features/admin/BroadcastsTab';
import { SiteUpdatesTab } from '@/components/features/admin/SiteUpdatesTab';

export type AnnouncementsSubtab = 'updates' | 'email';

const UPDATES_TAB_ID = 'admin-announcements-updates-tab';
const UPDATES_PANEL_ID = 'admin-announcements-updates-panel';
const EMAIL_TAB_ID = 'admin-announcements-email-tab';
const EMAIL_PANEL_ID = 'admin-announcements-email-panel';

const SUBTABS = [
  { key: 'updates', label: 'Updates', tabId: UPDATES_TAB_ID, panelId: UPDATES_PANEL_ID },
  { key: 'email', label: 'Email', tabId: EMAIL_TAB_ID, panelId: EMAIL_PANEL_ID },
] as const;

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

  return (
    <div className="mt-6 space-y-4">
      <AdminSubTabs
        label="Announcement channels"
        tabs={SUBTABS}
        active={activeSubtab}
        onSelect={onSelectSubtab}
      />

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
