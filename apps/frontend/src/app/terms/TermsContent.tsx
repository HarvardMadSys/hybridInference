'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { TERMS_SECTION_ANCHOR } from '@/site-ui/contract';
import { TermsSections } from '@/site-ui/terms-sections';

/**
 * The console's own terms page body: a card inside the console container.
 *
 * This is the *default*. A distribution changes the chrome through the Site UI
 * interface rather than by editing this file — `PublicRouteBoundary` renders the
 * deployment's `TermsFrame` around the same text when one is installed, and the
 * console's legal chrome otherwise. The words themselves live in
 * `@/site-ui/terms-sections`, so the two frames cannot drift apart.
 */
export function TermsContent(): JSX.Element {
  const branding = useBranding();
  const t = useT();

  return (
    <article className="mx-auto w-full max-w-3xl rounded-2xl border border-gray-200 bg-white px-6 py-8 shadow-sm sm:px-10 sm:py-10">
      <div className="border-b border-gray-200 pb-6">
        <h1 className="text-3xl font-bold tracking-tight text-gray-950 sm:text-4xl">
          {t('terms.header.title', 'Terms of Service')}
        </h1>
        <p className="mt-4 text-sm leading-6 text-gray-600">
          {fill(
            t(
              'terms.header.intro',
              'Last updated: June 20, 2026. These terms are a practical operating policy for using {app_name} and are not legal advice.',
            ),
            { app_name: branding.appName },
          )}
        </p>
      </div>

      <TermsSections anchorPrefix={TERMS_SECTION_ANCHOR} />
    </article>
  );
}
