import { redirect } from 'next/navigation';

interface PageProps {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}

export default async function AuditAdminPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (Array.isArray(value)) {
      value.forEach((v) => query.append(key, v));
    } else if (value !== undefined) {
      query.set(key, value);
    }
  }
  const qs = query.toString();
  redirect(qs ? `/dashboard/admin/log?${qs}` : '/dashboard/admin/log');
}
