'use client';

import { SITE_CONFIG_ERROR_DIGEST } from '@/config/site-config-error';

export default function GlobalError({
  error,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const configurationFailed = error.digest === SITE_CONFIG_ERROR_DIGEST;
  const title = configurationFailed
    ? 'Site configuration could not be loaded'
    : 'Unable to load the site';

  return (
    <html lang="en">
      <head>
        <title>{title}</title>
      </head>
      <body
        style={{
          margin: 0,
          background: '#f8fafc',
          color: '#111827',
          fontFamily: 'system-ui, sans-serif',
        }}
      >
        <main style={{ maxWidth: '32rem', margin: '15vh auto', padding: '2rem' }}>
          <h1 style={{ fontSize: '1.75rem', lineHeight: 1.25 }}>{title}</h1>
          <p style={{ color: '#4b5563', lineHeight: 1.6 }}>
            {configurationFailed
              ? 'The site settings are temporarily unavailable. Please retry. If the problem continues, contact the site administrator.'
              : 'Please retry. If the problem continues, contact the site administrator.'}
          </p>
          {/* A full navigation also retries root-layout and metadata loading. */}
          <a
            href=""
            style={{
              display: 'inline-block',
              marginTop: '1rem',
              padding: '0.75rem 1.25rem',
              borderRadius: '0.5rem',
              background: '#111827',
              color: '#fff',
              textDecoration: 'none',
            }}
          >
            Retry
          </a>
        </main>
      </body>
    </html>
  );
}
