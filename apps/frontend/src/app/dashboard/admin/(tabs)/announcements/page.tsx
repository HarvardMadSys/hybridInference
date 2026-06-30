import {
  AnnouncementsSubtab,
  AnnouncementsTab,
} from '@/components/features/admin/AnnouncementsTab';

interface PageProps {
  searchParams?: Promise<{ tab?: string }>;
}

export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  const initialSubtab: AnnouncementsSubtab = params?.tab === 'email' ? 'email' : 'updates';
  return <AnnouncementsTab initialSubtab={initialSubtab} />;
}
