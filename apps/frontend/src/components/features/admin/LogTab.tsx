import { AuditTab } from '@/components/features/admin/AuditTab';
import { AdminSubTabs } from '@/components/features/admin/AdminPrimitives';

const AUDIT_TAB_ID = 'admin-log-audit-tab';
const AUDIT_PANEL_ID = 'admin-log-audit-panel';

const LOG_TABS = [
  { key: 'audit', label: 'Audit Log', tabId: AUDIT_TAB_ID, panelId: AUDIT_PANEL_ID },
] as const;

export function LogTab() {
  return (
    <div className="mt-6 space-y-4">
      <AdminSubTabs label="Log sections" tabs={LOG_TABS} active="audit" />

      <div id={AUDIT_PANEL_ID} role="tabpanel" aria-labelledby={AUDIT_TAB_ID}>
        <AuditTab />
      </div>
    </div>
  );
}
