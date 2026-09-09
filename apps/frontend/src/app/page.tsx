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

export default function HomePage(): JSX.Element {
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
        <p>Service is provided without guarantee.</p>
        <p className="mt-1">
          {branding.dataPolicyNotice ? `${branding.dataPolicyNotice} ` : null}
          See our{' '}
          <Link href="/terms" className="underline hover:text-gray-700">
            Terms of Service
          </Link>
          .
        </p>
      </footer>
    </div>
  );
}
