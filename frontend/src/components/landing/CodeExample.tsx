'use client';

import { useState } from 'react';

const CURL_EXAMPLE = `curl https://freeinference.org/v1/chat/completions \\
  -H "Authorization: Bearer $FREEINFERENCE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`;

export function CodeExample(): JSX.Element {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(CURL_EXAMPLE);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable; do nothing.
    }
  }

  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          One <code className="font-mono text-crimson">curl</code> away
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Use the same OpenAI client libraries you already know.
        </p>
      </div>

      <div className="relative mt-8 overflow-hidden rounded-xl bg-gray-900 shadow-card">
        <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
          <span className="font-mono text-xs uppercase tracking-wider text-gray-400">bash</span>
          <button
            type="button"
            onClick={handleCopy}
            className="rounded-md border border-gray-700 px-3 py-1 text-xs font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            aria-live="polite"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
        <pre className="overflow-x-auto px-4 py-4 text-sm leading-relaxed text-gray-100">
          <code>{CURL_EXAMPLE}</code>
        </pre>
      </div>
    </section>
  );
}
