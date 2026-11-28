import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import { config } from '@/config/env';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
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

/**
 * The fleet page, or a redirect to another deployment's when this one links
 * there (`OPENINFRA_PUBLIC_URL`): a staging site shows production's fleet
 * rather than an empty page of its own.
 */
export default async function OpenInfraPage(): Promise<JSX.Element> {
  const { openinfraUrl } = await loadRuntimeSiteConfig();
  if (openinfraUrl) redirect(openinfraUrl);
  return <OpenInfraContent />;
}
