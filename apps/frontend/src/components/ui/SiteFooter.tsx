'use client';

import Link from 'next/link';
import { BuildInfo } from '@/components/ui/BuildInfo';
import { useBranding } from '@/components/providers/SiteConfigProvider';

export function SiteFooter(): JSX.Element {
  const branding = useBranding();
  return (
    <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
      <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
        <span>© {branding.appName}</span>
        <span aria-hidden="true">·</span>
        <a
          href={branding.orgUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          {branding.orgName}
        </a>
        <span aria-hidden="true">·</span>
        <a
          href={branding.docsUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Docs
        </a>
        <span aria-hidden="true">·</span>
        <a
          href={branding.statusUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Status
        </a>
        <span aria-hidden="true">·</span>
        <Link href="/terms" className="hover:text-crimson">
          Terms
        </Link>
        {branding.team.length > 0 && (
          <>
            <span aria-hidden="true">·</span>
            <Link href="/team" className="hover:text-crimson">
              Team
            </Link>
          </>
        )}
        <span aria-hidden="true">·</span>
        <a
          href={branding.githubUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          GitHub
        </a>
        <span aria-hidden="true">·</span>
        <BuildInfo />
      </div>
    </footer>
  );
}
