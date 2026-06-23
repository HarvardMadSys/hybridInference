import { redirect } from 'next/navigation';

export default function UpdatesAdminPage() {
  redirect('/dashboard/admin/settings?tab=updates');
}
