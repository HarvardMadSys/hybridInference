import type { Metadata } from 'next';
import { config } from '@/config/env';
import { requestTranslator } from '@/lib/i18n/request.server';
import { fill } from '@/lib/utils/interpolate';
import { OpenInfraContent } from './OpenInfraContent';

export async function generateMetadata(): Promise<Metadata> {
  const t = await requestTranslator();
  return {
    title: fill(t('openinfra.meta_title', 'OpenInfra | {app_name}'), {
      app_name: config.appName,
    }),
    description: fill(
      t(
        'openinfra.meta_description',
        'Live GPU, host and inference-engine metrics from the servers behind {app_name}.',
      ),
      { app_name: config.appName },
    ),
  };
}

export default function OpenInfraPage(): JSX.Element {
  return <OpenInfraContent />;
}
