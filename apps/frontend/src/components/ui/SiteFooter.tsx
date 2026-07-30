'use client';

import Link from 'next/link';
import { Fragment } from 'react';
import { BuildInfo } from '@/components/ui/BuildInfo';
import { useBranding } from '@/components/providers/SiteConfigProvider';

export function SiteFooter(): JSX.Element {
  const branding = useBranding();

  // Built as a list so a deployment that has no operating organization, docs
  // site or status page simply shows fewer entries — rendering an empty href
  // (or a stranded separator) is the failure mode this replaces.
  const entries: { key: string; node: JSX.Element }[] = [
    { key: 'copyright', node: <span>© {branding.appName}</span> },
  ];

  if (branding.orgName && branding.orgUrl) {
    entries.push({
      key: 'org',
      node: (
        <a
          href={branding.orgUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          {branding.orgName}
        </a>
      ),
    });
  }

  if (branding.docsUrl) {
    entries.push({
      key: 'docs',
      node: (
        <a
          href={branding.docsUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Docs
        </a>
      ),
    });
  }

  if (branding.statusUrl) {
    entries.push({
      key: 'status',
      node: (
        <a
          href={branding.statusUrl}
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Status
        </a>
      ),
    });
  }

  entries.push({
    key: 'terms',
    node: (
      <Link href="/terms" prefetch={false} className="hover:text-crimson">
        Terms
      </Link>
    ),
  });

  if (branding.team.length > 0) {
    entries.push({
      key: 'team',
      node: (
        <Link href="/team" prefetch={false} className="hover:text-crimson">
          Team
        </Link>
      ),
    });
  }

  entries.push({
    key: 'github',
    node: (
      <a
        href={branding.githubUrl}
        className="hover:text-crimson"
        target="_blank"
        rel="noopener noreferrer"
      >
        GitHub
      </a>
    ),
  });

  entries.push({ key: 'build', node: <BuildInfo /> });

  return (
    <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
      <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
        {entries.map((entry, index) => (
          <Fragment key={entry.key}>
            {index > 0 && <span aria-hidden="true">·</span>}
            {entry.node}
          </Fragment>
        ))}
      </div>
    </footer>
  );
}
