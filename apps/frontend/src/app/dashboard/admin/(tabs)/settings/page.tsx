import { redirect } from 'next/navigation';

import { ADMIN_MODELS_PATH } from '@/components/features/configuration/paths';

import { SettingsTab } from '../../SettingsTab';
import { settingsTabKey } from '../../settingsLayout';

interface PageProps {
  searchParams?: Promise<{ tab?: string; missing?: string }>;
}

// `?tab=` opens a sub-tab. `?missing=1` is where the "required settings are
// missing" banner links: the page opens filtered to what needs a value.
export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  const missingOnly = params?.missing === '1';
  // The Models sub-tab became Providers → Models. An old link to it lands there,
  // unless it asked for the missing settings, which only this tab lists: then
  // on Application, which kept the sub-tab's other documents.
  const legacyModels = params?.tab === 'models';
  if (legacyModels && !missingOnly) redirect(ADMIN_MODELS_PATH);
  return (
    <SettingsTab
      initialTab={settingsTabKey(legacyModels ? 'application' : params?.tab)}
      initialMissingOnly={missingOnly}
    />
  );
}
