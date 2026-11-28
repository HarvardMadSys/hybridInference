import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { requestTranslator } from '@/lib/i18n/request.server';
import { fill } from '@/lib/utils/interpolate';
import { OpenInfraContent } from './OpenInfraContent';

export async function generateMetadata(): Promise<Metadata> {
  // The site's name as the deployment configures it now, not the one the
  // image was built with: a neutral image is built as HybridInference.
  const [{ branding }, t] = await Promise.all([loadRuntimeSiteConfig(), requestTranslator()]);
  const name = { app_name: branding.appName };
  return {
    title: fill(t('openinfra.meta_title', 'OpenInfra | {app_name}'), name),
    description: fill(
      t(
        'openinfra.meta_description',
        'Live GPU, host and inference-engine metrics from the servers behind {app_name}.',
      ),
      name,
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
