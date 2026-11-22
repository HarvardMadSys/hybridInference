'use client';

import { useEffect, useState } from 'react';

function faviconUrl(serverUrl: string): string {
  try {
    const url = new URL(serverUrl);
    // The page policy only allows https images, so an http favicon is a failure.
    return url.protocol === 'https:' ? `${url.origin}/favicon.ico` : '';
  } catch {
    return '';
  }
}

/**
 * A server's own favicon, falling back to its initial when the host serves
 * none. No third-party icon service: the browser asks the server directly.
 */
export function McpServerIcon({
  name,
  url,
  iconUrl = '',
  size = 'md',
}: {
  name: string;
  url: string;
  iconUrl?: string;
  size?: 'sm' | 'md' | 'lg';
}) {
  const src = iconUrl || faviconUrl(url);
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [src]);

  const box =
    size === 'lg' ? 'h-11 w-11 text-sm' : size === 'sm' ? 'h-7 w-7 text-[10px]' : 'h-9 w-9 text-xs';

  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center overflow-hidden rounded-lg border border-gray-200 bg-white font-semibold text-gray-600 ${box}`}
    >
      {src && !failed ? (
        // eslint-disable-next-line @next/next/no-img-element -- server favicons are arbitrary hosts
        <img
          src={src}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          className="h-full w-full object-contain p-1"
          onError={() => setFailed(true)}
        />
      ) : (
        name.slice(0, 1).toUpperCase()
      )}
    </span>
  );
}
