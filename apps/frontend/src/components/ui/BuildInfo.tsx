import React from 'react';
import { branding } from '@/config/branding';
import { config } from '@/config/env';

const COMMIT_URL_BASE = branding.commitUrlBase;

function formatTimestamp(iso: string): string {
  return `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

export function BuildInfo(): JSX.Element {
  const { buildSha, buildTimestamp } = config;

  const shaPart = buildSha ? (
    <>
      build{' '}
      <a
        href={`${COMMIT_URL_BASE}/${buildSha}`}
        target="_blank"
        rel="noreferrer"
        className="hover:underline"
      >
        {buildSha.slice(0, 7)}
      </a>
    </>
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
