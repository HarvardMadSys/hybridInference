import { redirect } from 'next/navigation';

// Token Usage moved into Analytics; an old link still lands on it.
export default function TokenUsageAdminPage() {
  redirect('/dashboard/admin/analytics/token-usage');
}
