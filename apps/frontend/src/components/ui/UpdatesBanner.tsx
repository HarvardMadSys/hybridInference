'use client';

import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getPublicSiteUpdates, type PublicSiteUpdate } from '@/lib/api/updates';
import { branding } from '@/config/branding';

const DISMISS_KEY = `${branding.storageKeyPrefix}:dismissed-banner`;

export function UpdatesBanner(): JSX.Element | null {
  const [banner, setBanner] = useState<PublicSiteUpdate | null>(null);
  const [dismissed, setDismissed] = useState(true);

  useEffect(() => {
    let active = true;
    getPublicSiteUpdates().then((res) => {
      if (!active || !res.banner) return;
      setBanner(res.banner);
      // Banners are dismissed per-id, so publishing a new banner re-shows it.
      const stored = localStorage.getItem(DISMISS_KEY);
      setDismissed(stored === res.banner.id);
    });
    return () => {
      active = false;
    };
  }, []);

  if (!banner || dismissed) return null;

  function dismiss() {
    if (banner) localStorage.setItem(DISMISS_KEY, banner.id);
    setDismissed(true);
  }

  return (
    <div className="relative w-full rounded-2xl border border-crimson/20 bg-crimson/5 px-5 py-3 pr-12 text-sm text-gray-800 shadow-subtle">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-semibold text-crimson">{banner.title}</span>
        {banner.body && (
          <span className="[&_a]:text-crimson [&_a]:underline">
            {/* Render markdown paragraphs as fragments so the banner stays inline:
                a <p> nested in this <span> is invalid HTML and triggers a Next.js
                hydration mismatch. */}
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{ p: ({ children }) => <>{children}</> }}
            >
              {banner.body}
            </ReactMarkdown>
          </span>
        )}
        {banner.link_url && (
          <a
            href={banner.link_url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-crimson underline"
          >
            {banner.link_label || 'Learn more'}
          </a>
        )}
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss announcement"
        className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1 text-crimson/60 transition-colors hover:bg-crimson/10 hover:text-crimson"
      >
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
          <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
        </svg>
      </button>
    </div>
  );
}
