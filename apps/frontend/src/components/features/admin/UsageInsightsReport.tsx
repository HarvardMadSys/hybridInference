'use client';

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { UsageInsightsResponse, UsageInsightsSamplesResponse } from '@/lib/api/admin';

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
      <p
        className="mt-5 border-t border-gray-100 pt-3 text-[11px] text-gray-400"
        suppressHydrationWarning
      >
        Generated {new Date(result.generated_at).toLocaleString()} · model {result.model} ·{' '}
        {result.sampled_requests} sampled request(s) · scope {result.scope}
      </p>
    </div>
  );
}

function formatSampleTime(value: string | null): string {
  if (!value) return 'unknown time';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/** Renders the raw user-turn sample Analyze usage would send to the LLM. */
export function UsageInsightsSampleList({ result }: { result: UsageInsightsSamplesResponse }) {
  return (
    <div className="max-h-[28rem] overflow-y-auto rounded-xl border border-gray-200 bg-white">
      <div className="sticky top-0 border-b border-gray-100 bg-white px-4 py-2 text-[11px] text-gray-500">
        {result.sampled_requests} sampled request{result.sampled_requests === 1 ? '' : 's'} · user
        turns only · scope {result.scope}
      </div>
      <ol className="divide-y divide-gray-100">
        {result.samples.map((sample, i) => (
          <li
            key={`${sample.timestamp ?? 'undated'}-${sample.model_id ?? 'model'}-${i}`}
            className="px-4 py-3"
          >
            <div className="text-[11px] text-gray-500">
              <span className="font-medium text-gray-700">Request {i + 1}</span>
              {' · '}
              <span suppressHydrationWarning>{formatSampleTime(sample.timestamp)}</span>
              {sample.model_id && (
                <>
                  {' · '}
                  {sample.model_id}
                  {sample.provider ? ` (${sample.provider})` : ''}
                </>
              )}
            </div>
            {sample.user_agent && (
              <div className="mt-0.5 truncate text-[11px] text-gray-400" title={sample.user_agent}>
                {sample.user_agent}
              </div>
            )}
            {sample.system_opener && (
              <div className="mt-1 text-[12px] text-gray-500">
                <span className="font-medium text-gray-400">system</span>{' '}
                <span className="whitespace-pre-wrap break-words">{sample.system_opener}</span>
              </div>
            )}
            {sample.user_messages.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {sample.user_messages.map((msg, j) => (
                  <li key={j} className="text-[12px] leading-relaxed text-gray-800">
                    <span className="font-medium text-gray-400">user</span>{' '}
                    <span className="whitespace-pre-wrap break-words">{msg}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="mt-1 text-[12px] italic text-gray-400">user: none captured</div>
            )}
          </li>
        ))}
      </ol>
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
