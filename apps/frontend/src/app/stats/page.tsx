import type { Metadata } from 'next';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { requestTranslator } from '@/lib/i18n/request.server';
import { fill } from '@/lib/utils/interpolate';
import { StatsContent } from './StatsContent';

export async function generateMetadata(): Promise<Metadata> {
  // The site's name as the deployment configures it now, not the one the
  // image was built with: a neutral image is built as HybridInference.
  const [{ branding }, t] = await Promise.all([loadRuntimeSiteConfig(), requestTranslator()]);
  const name = { app_name: branding.appName };
  return {
    title: fill(t('stats.meta_title', 'Usage stats | {app_name}'), name),
    description: fill(
      t(
        'stats.meta_description',
        'Tokens served, countries, languages and agents on {app_name}, refreshed daily.',
      ),
      name,
    ),
  };
}

export default function StatsPage(): JSX.Element {
  return <StatsContent />;
}
