import { redirect } from 'next/navigation';

export default function AuditAdminPage() {
  redirect('/dashboard/admin/log');
}
