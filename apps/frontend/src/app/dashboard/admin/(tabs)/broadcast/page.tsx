import { redirect } from 'next/navigation';

export default function BroadcastAdminPage() {
  redirect('/dashboard/admin/announcements?tab=email');
}
