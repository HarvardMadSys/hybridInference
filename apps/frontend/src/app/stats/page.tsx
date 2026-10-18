import type { Metadata } from 'next';
import { config } from '@/config/env';
import { requestTranslator } from '@/lib/i18n/request.server';
import { fill } from '@/lib/utils/interpolate';
import { StatsContent } from './StatsContent';

export async function generateMetadata(): Promise<Metadata> {
  const t = await requestTranslator();
  return {
    title: fill(t('stats.meta_title', 'Usage stats | {app_name}'), { app_name: config.appName }),
    description: fill(
      t(
        'stats.meta_description',
        'Tokens served, countries, languages and agents on {app_name}, refreshed daily.',
      ),
      { app_name: config.appName },
    ),
  };
}

export default function StatsPage(): JSX.Element {
  return <StatsContent />;
}
