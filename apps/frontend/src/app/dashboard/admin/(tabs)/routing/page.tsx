import { redirect } from 'next/navigation';

export default function RoutingAdminPage() {
  redirect('/dashboard/admin/settings?tab=routing');
}
