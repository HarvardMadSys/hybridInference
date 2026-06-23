import { AuditTab } from '@/components/features/admin/AuditTab';

const AUDIT_TAB_ID = 'admin-log-audit-tab';
const AUDIT_PANEL_ID = 'admin-log-audit-panel';

export function LogTab() {
  return (
    <div className="mt-5 space-y-6">
      <div
        className="flex flex-wrap items-center gap-1 border-b border-gray-200 pb-2"
        role="tablist"
      >
        <button
          type="button"
          id={AUDIT_TAB_ID}
          role="tab"
          aria-selected
          aria-controls={AUDIT_PANEL_ID}
          className="rounded-md bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white transition"
        >
          Audit Log
        </button>
      </div>

      <div id={AUDIT_PANEL_ID} role="tabpanel" aria-labelledby={AUDIT_TAB_ID}>
        <AuditTab />
      </div>
    </div>
  );
}
