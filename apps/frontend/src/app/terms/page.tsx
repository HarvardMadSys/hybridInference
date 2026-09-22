import type { Metadata } from 'next';
import { fill } from '@/lib/utils/interpolate';
import { translate } from '@/lib/i18n/translate';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { pageMetadata } from '@/config/site-metadata';
import { TermsPageContent } from '@/site-ui/SiteUiBoundary';

export async function generateMetadata(): Promise<Metadata> {
  const siteConfig = await loadRuntimeSiteConfig();
  // The tab title is rendered copy, so it follows the same document as the
  // page body: `translator` is a pure function, which is what lets a server
  // component resolve slots without the client hook.
  const t = translate;
  return pageMetadata(
    siteConfig,
    t('meta.terms.title', 'Terms of Service'),
    fill(t('meta.terms.description', 'Terms of Service for {app_name}.'), {
      app_name: siteConfig.branding.appName,
    }),
  );
}

export default function TermsPage(): JSX.Element {
  // The *frame* is the Site UI's business: the module's `TermsFrame` when it
  // supplies one, the console's own card otherwise. Chosen in
  // `TermsPageContent`, which is also what decides whether the console
  // container wraps this page — so the legal text and its slots are identical
  // either way, and this page names no layout at all.
  return <TermsPageContent />;
}
