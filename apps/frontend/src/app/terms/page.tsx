import type { Metadata } from 'next';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { pageMetadata } from '@/config/site-metadata';
import { TermsContent } from './TermsContent';

export async function generateMetadata(): Promise<Metadata> {
  const siteConfig = await loadRuntimeSiteConfig();
  return pageMetadata(
    siteConfig,
    'Terms of Service',
    `Terms of Service for ${siteConfig.branding.appName}.`,
  );
}

export default function TermsPage(): JSX.Element {
  return <TermsContent />;
}
