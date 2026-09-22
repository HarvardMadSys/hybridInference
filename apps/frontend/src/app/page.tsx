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
 * The console's own landing page.
 *
 * A distribution that ships its own replaces this one at build time, through
 * the Site UI interface: `SiteUiBoundary` renders the module's `Landing` for
 * `/` and this component is never reached. There is no runtime flag here — the
 * decision was made when the image was built, which is the difference between
 * this and the `presentation.preset` branch it replaces.
 */
export default function HomePage(): JSX.Element {
  return <ClassicLanding />;
}
