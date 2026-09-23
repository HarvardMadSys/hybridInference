'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';
import { TERMS_SECTION_ANCHOR } from '@/site-ui/contract';
import { TermsSections } from '@/site-ui/terms-sections';

/**
 * The console's own terms page body: a card inside the console container.
 *
 * This is the *default*, and not the Site UI export of the same name. A
 * distribution that publishes its own legal text does it through the interface
 * rather than by editing this file: its `TermsFrame` and `TermsContent` replace
 * this card, and the console's sign-up step shows that `TermsContent` too. The
 * console's words live in `@/site-ui/terms-sections`, which this card and the
 * console's sign-up step share, so those two cannot drift apart either.
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
