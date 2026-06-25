'use client';

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { UsageInsightsResponse } from '@/lib/api/admin';

const MARKDOWN_CLASS =
  'text-[13px] leading-relaxed text-gray-700 [&_a]:text-blue-600 [&_a]:underline ' +
  '[&_h1]:mt-4 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:mt-4 [&_h2]:text-base [&_h2]:font-semibold ' +
  '[&_h3]:mt-3 [&_h3]:text-sm [&_h3]:font-semibold [&_p]:my-2 [&_strong]:font-semibold ' +
  '[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 ' +
  '[&_li]:my-0.5 [&_code]:rounded [&_code]:bg-gray-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px]';

/** Renders a Usage Insights analysis report (Markdown + provenance footer). */
export function UsageInsightsReport({ result }: { result: UsageInsightsResponse }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-6">
      <div className={MARKDOWN_CLASS}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.analysis}</ReactMarkdown>
      </div>
      <p className="mt-5 border-t border-gray-100 pt-3 text-[11px] text-gray-400">
        Generated {new Date(result.generated_at).toLocaleString()} · model {result.model} ·{' '}
        {result.sampled_requests} sampled request(s) · scope {result.scope}
      </p>
    </div>
  );
}

/** A pulsing placeholder shown while an analysis is running. */
export function UsageInsightsReportSkeleton() {
  return (
    <div className="rounded-xl border border-gray-100 bg-gray-50 p-5">
      <div className="mb-3 h-2.5 w-40 animate-pulse rounded bg-gray-200" />
      <div className="space-y-2">
        <div className="h-3 w-full animate-pulse rounded bg-gray-200" />
        <div className="h-3 w-11/12 animate-pulse rounded bg-gray-200" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-gray-200" />
      </div>
    </div>
  );
}
