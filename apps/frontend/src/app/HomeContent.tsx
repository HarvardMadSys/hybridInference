'use client';

import Link from 'next/link';
import {
  CodeExample,
  DeveloperHome,
  Features,
  Hero,
  HowItWorks,
  Sponsors,
  Updates,
  UseCases,
} from '@/components/landing';
import { UpdatesBanner } from '@/components/ui/UpdatesBanner';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { SITE_UI_CLIENT } from '@/site-ui/module';

/** The console's own landing page, unchanged for deployments that keep it. */
function ClassicLanding(): JSX.Element {
  const t = useT();
  const { branding, distribution } = useSiteConfig();

  // The developer home belongs to the runnable example. Other deployments
  // retain their existing landing page, including FreeInference.
  const isExample = distribution.id === 'example';

  return (
    <div className={`flex w-full flex-col ${isExample ? 'gap-6' : 'gap-4'}`}>
      <UpdatesBanner />
      {isExample ? <DeveloperHome /> : <Hero />}
      <Updates />
      {!isExample && (
        <>
          <Features />
          <UseCases />
          <HowItWorks />
          <CodeExample />
        </>
      )}
      <Sponsors />
      <footer
        className={`${isExample ? 'mt-10' : 'mt-16'} border-t border-gray-200 px-4 py-8 text-center text-xs text-gray-500 sm:px-6 lg:px-8`}
      >
        <p>{t('chrome.footer.no_warranty', 'Service is provided without guarantee.')}</p>
        <p className="mt-1">
          {branding.dataPolicyNotice ? `${branding.dataPolicyNotice} ` : null}
          {t('chrome.footer.terms_lead', 'See our')}{' '}
          <Link href="/terms" className="underline hover:text-gray-700">
            {t('chrome.footer.terms_link', 'Terms of Service')}
          </Link>
          .
        </p>
      </footer>
    </div>
  );
}

/**
 * The home page: the compiled-in module's `Landing` when it has one, the
 * console's own landing page otherwise.
 *
 * There is no runtime flag here — the decision was made when the image was
 * built, which is the difference between this and the `presentation.preset`
 * branch it replaces.
 *
 * Rendered here, in the page, rather than by the root layout in place of the
 * page: a module's landing page that throws is then caught by the route's
 * error boundary (`app/error.tsx`), which the root layout sits above, and the
 * rest of the site keeps working.
 */
export function HomeContent(): JSX.Element {
  const Landing = SITE_UI_CLIENT.Landing;
  return Landing ? <Landing /> : <ClassicLanding />;
}
