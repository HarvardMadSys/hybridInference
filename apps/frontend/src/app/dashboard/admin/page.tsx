import { redirect } from 'next/navigation';

const VALID_TABS = new Set([
  'users',
  'audit',
  'broadcast',
  'requests',
  'providers',
  'performance',
  'analytics',
  'token-usage',
  'settings',
]);

const TAB_ALIASES: Record<string, string> = {
  broadcasts: 'broadcast',
  'provider-performance': 'performance',
};

export default async function AdminPage({
  searchParams,
}: {
  searchParams: Promise<{ tab?: string }>;
}) {
  const { tab } = await searchParams;
  const target = tab ? (TAB_ALIASES[tab] ?? tab) : 'users';
  const slug = VALID_TABS.has(target) ? target : 'users';
  redirect(`/dashboard/admin/${slug}`);
}
