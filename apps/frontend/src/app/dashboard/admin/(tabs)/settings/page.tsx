import { SettingsTab } from '../../SettingsTab';
import { settingsTabKey } from '../../settingsLayout';

interface PageProps {
  searchParams?: Promise<{ tab?: string; missing?: string }>;
}

// `?tab=` opens a sub-tab. `?missing=1` is where the "required settings are
// missing" banner links: the page opens filtered to what needs a value.
export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  return (
    <SettingsTab
      initialTab={settingsTabKey(params?.tab)}
      initialMissingOnly={params?.missing === '1'}
    />
  );
}
