'use client';

import { useState } from 'react';

import { useBranding } from '@/components/providers/SiteConfigProvider';
import { buildCurlExample } from './curlExample';
import { useT } from '@/components/providers/useT';

export function CodeExample(): JSX.Element | null {
  const [copied, setCopied] = useState(false);
  const branding = useBranding();
  const t = useT();

  if (!branding.exampleApiBase) {
    return null;
  }

  const curlExample = buildCurlExample(branding);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(curlExample);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable; do nothing.
    }
  }

  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <p className="text-sm font-semibold uppercase tracking-wider text-crimson">
          {t('landing.quickstart.eyebrow', 'Quickstart')}
        </p>
        <h2 className="mt-2 font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          {t('landing.quickstart.title_lead', 'One')}{' '}
          <code className="font-mono text-crimson">curl</code>{' '}
          {t('landing.quickstart.title_tail', 'away')}
        </h2>
        <p className="mt-3 text-base text-gray-600">
          {t(
            'landing.quickstart.subtitle',
            'Use the same OpenAI client libraries you already know.',
          )}
        </p>
      </div>

      <div className="relative mx-auto mt-10 max-w-3xl overflow-hidden rounded-2xl border border-gray-800 bg-gray-900 shadow-card ring-1 ring-white/5">
        <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1.5" aria-hidden>
              <span className="h-3 w-3 rounded-full bg-red-400/80" />
              <span className="h-3 w-3 rounded-full bg-yellow-400/80" />
              <span className="h-3 w-3 rounded-full bg-green-400/80" />
            </span>
            <span className="font-mono text-xs uppercase tracking-wider text-gray-400">bash</span>
          </div>
          <button
            type="button"
            onClick={handleCopy}
            className="rounded-md border border-gray-700 px-3 py-1 text-xs font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            aria-live="polite"
          >
            {copied
              ? t('landing.quickstart.copied', 'Copied!')
              : t('landing.quickstart.copy', 'Copy')}
          </button>
        </div>
        <pre className="overflow-x-auto px-4 py-4 text-sm leading-relaxed text-gray-100">
          <code>{curlExample}</code>
        </pre>
      </div>
    </section>
  );
}
