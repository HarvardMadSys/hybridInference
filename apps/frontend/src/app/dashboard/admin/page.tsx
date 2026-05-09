import { redirect } from 'next/navigation';

export default function AdminIndexPage() {
  redirect('/dashboard/admin/users');
}
