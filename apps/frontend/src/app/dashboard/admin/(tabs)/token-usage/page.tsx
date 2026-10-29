import { redirect } from 'next/navigation';

// Token Usage moved into the Analytics overview; an old link still lands on it.
export default function TokenUsageAdminPage() {
  redirect('/dashboard/admin/analytics');
}
