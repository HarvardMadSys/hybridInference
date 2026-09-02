'use client';

import React from 'react';
import { config } from '@/config/env';
import { useBranding } from '@/components/providers/SiteConfigProvider';

function formatTimestamp(iso: string): string {
  return `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

export function BuildInfo(): JSX.Element {
  const { buildSha, buildTimestamp } = config;
  const { commitUrlBase } = useBranding();

  const shaPart =
    buildSha && commitUrlBase ? (
      <>
        build{' '}
        <a
          href={`${commitUrlBase}/${buildSha}`}
          target="_blank"
          rel="noreferrer"
          className="hover:underline"
        >
          {buildSha.slice(0, 7)}
        </a>
      </>
    ) : buildSha ? (
      <>build {buildSha.slice(0, 7)}</>
    ) : (
      <>build dev</>
    );

  const timePart = buildTimestamp ? (
    <time dateTime={buildTimestamp}>deployed {formatTimestamp(buildTimestamp)}</time>
  ) : (
    <>local build</>
  );

  return (
    <span>
      {shaPart} · {timePart}
    </span>
  );
}
