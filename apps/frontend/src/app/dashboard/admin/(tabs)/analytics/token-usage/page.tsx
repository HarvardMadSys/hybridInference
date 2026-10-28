import { redirect } from 'next/navigation';

// Token usage is now the top of the Analytics overview; an old link still lands on it.
export default function TokenUsageAnalyticsPage() {
  redirect('/dashboard/admin/analytics');
}
