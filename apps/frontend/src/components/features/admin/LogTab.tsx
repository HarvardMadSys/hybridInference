import { AuditTab } from '@/components/features/admin/AuditTab';

// The audit log is the Log tab's only content, so it is shown directly rather
// than behind a one-item sub-tab bar.
export function LogTab() {
  return (
    <div className="mt-6">
      <AuditTab />
    </div>
  );
}
