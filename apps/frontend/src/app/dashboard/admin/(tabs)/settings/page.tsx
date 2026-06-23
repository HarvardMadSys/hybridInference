import { SettingsSubtab, SettingsTab } from '../../SettingsTab';

interface PageProps {
  searchParams?: Promise<{ tab?: string }>;
}

export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  const tab = params?.tab;
  const initialSubtab: SettingsSubtab =
    tab === 'updates' || tab === 'routing' || tab === 'routewise' ? tab : 'general';
  return <SettingsTab initialSubtab={initialSubtab} />;
}
