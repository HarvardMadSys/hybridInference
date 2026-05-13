import { SettingsSubtab, SettingsTab } from '../../SettingsTab';

interface PageProps {
  searchParams?: Promise<{ tab?: string }>;
}

export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  const initialSubtab: SettingsSubtab =
    params?.tab === 'routing'
      ? 'routing'
      : params?.tab === 'routewise'
        ? 'routewise'
        : 'general';
  return <SettingsTab initialSubtab={initialSubtab} />;
}
