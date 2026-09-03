'use client';

import Link from 'next/link';
import { DeveloperHome, Sponsors, Updates } from '@/components/landing';
import { UpdatesBanner } from '@/components/ui/UpdatesBanner';
import { useBranding } from '@/components/providers/SiteConfigProvider';

// The homepage is the developer home for every deployment: a fresh clone is
// nobody's gateway but its operator's, so it ships no marketing copy. Updates,
// sponsors and the data-policy notice are distribution content and render only
// when the deployment supplies them.
export default function HomePage(): JSX.Element {
  const branding = useBranding();

  return (
    <div className="flex w-full flex-col gap-6">
      <UpdatesBanner />
      <DeveloperHome />
      <Updates />
      <Sponsors />
      <footer className="mt-10 border-t border-gray-200 px-4 py-8 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
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
