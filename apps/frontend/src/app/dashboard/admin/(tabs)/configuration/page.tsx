import { redirect } from 'next/navigation';

import { ADMIN_SETTINGS_PATH } from '@/components/features/configuration/paths';

interface PageProps {
  searchParams?: Promise<{ missing?: string }>;
}

// The configuration moved into Admin → Settings. An old link still lands
// there, still filtered to what needs a value when it asked for that.
export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  redirect(params?.missing === '1' ? `${ADMIN_SETTINGS_PATH}?missing=1` : ADMIN_SETTINGS_PATH);
}
