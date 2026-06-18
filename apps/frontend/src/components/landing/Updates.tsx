'use client';

import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getPublicSiteUpdates, type PublicSiteUpdate } from '@/lib/api/updates';

function fmtDate(s: string): string {
  return new Date(s).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

export function Updates(): JSX.Element | null {
  const [updates, setUpdates] = useState<PublicSiteUpdate[]>([]);

  useEffect(() => {
    let active = true;
    getPublicSiteUpdates().then((res) => {
      if (active) setUpdates(res.updates);
    });
    return () => {
      active = false;
    };
  }, []);

  if (updates.length === 0) return null;

  return (
    <section className="px-4 py-6 sm:px-6 lg:px-8" aria-label="updates">
      <div className="mx-auto w-full max-w-4xl rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
        <p className="text-center text-sm font-semibold uppercase tracking-wide text-gray-500">
          Latest Updates
        </p>
        <ul className="mt-6 space-y-6">
          {updates.map((u) => (
            <li key={u.id} className="border-l-2 border-crimson/30 pl-4">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <h3 className="font-serif text-lg font-semibold text-gray-900">{u.title}</h3>
                <time className="text-xs text-gray-400">{fmtDate(u.created_at)}</time>
              </div>
              {u.body && (
                <div className="mt-1 text-sm text-gray-600 [&_a]:text-crimson [&_a]:underline [&_h2]:mt-2 [&_h2]:text-base [&_h2]:font-semibold [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{u.body}</ReactMarkdown>
                </div>
              )}
              {u.link_url && (
                <a
                  href={u.link_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-block text-sm font-medium text-crimson underline"
                >
                  {u.link_label || 'Learn more'}
                </a>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
