'use client';

import { useCallback, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { analyzeUsageInsights, UsageInsightsResponse } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

// The freeinference.org API key is sensitive; we keep it only in the browser
// (localStorage) so the admin doesn't have to re-paste it every visit, and send
// it per-request. It is never persisted server-side.
const KEY_STORAGE = 'usage_insights_api_key';
const MODEL_STORAGE = 'usage_insights_model';
const DEFAULT_MODEL = 'glm-5.2';
const DEFAULT_BASE_URL = 'https://freeinference.org/v1';
const DEFAULT_LIMIT = 40;
const MIN_LIMIT = 1;
const MAX_LIMIT = 200;

// Parse the free-form limit input into a valid sample size, falling back to the
// default when it's blank or non-numeric.
function clampLimit(raw: string): number {
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return DEFAULT_LIMIT;
  return Math.max(MIN_LIMIT, Math.min(MAX_LIMIT, n));
}

const MARKDOWN_CLASS =
  'text-[13px] leading-relaxed text-gray-700 [&_a]:text-blue-600 [&_a]:underline ' +
  '[&_h1]:mt-4 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:mt-4 [&_h2]:text-base [&_h2]:font-semibold ' +
  '[&_h3]:mt-3 [&_h3]:text-sm [&_h3]:font-semibold [&_p]:my-2 [&_strong]:font-semibold ' +
  '[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 ' +
  '[&_li]:my-0.5 [&_code]:rounded [&_code]:bg-gray-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px]';

export function UsageInsightsTab() {
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [userEmail, setUserEmail] = useState('');
  // Kept as a string so the field can be cleared / edited freely; clamped to a
  // valid number on blur and when the request is built.
  const [limit, setLimit] = useState(String(DEFAULT_LIMIT));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UsageInsightsResponse | null>(null);

  // Restore the remembered key/model on mount.
  useEffect(() => {
    try {
      const k = window.localStorage.getItem(KEY_STORAGE);
      const m = window.localStorage.getItem(MODEL_STORAGE);
      if (k) setApiKey(k);
      if (m) setModel(m);
    } catch {
      // localStorage unavailable (private mode / SSR) — ignore.
    }
  }, []);

  const analyze = useCallback(async () => {
    if (!apiKey.trim()) {
      setError('Enter a freeinference.org API key to run the analysis.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      try {
        window.localStorage.setItem(KEY_STORAGE, apiKey.trim());
        window.localStorage.setItem(MODEL_STORAGE, model.trim() || DEFAULT_MODEL);
      } catch {
        // ignore persistence failures
      }
      const res = await analyzeUsageInsights({
        api_key: apiKey.trim(),
        model: model.trim() || DEFAULT_MODEL,
        base_url: DEFAULT_BASE_URL,
        user_email: userEmail.trim() || undefined,
        limit: clampLimit(limit),
      });
      setResult(res);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [apiKey, model, userEmail, limit]);

  return (
    <div className="mt-6 space-y-6">
      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <h2 className="text-[15px] font-semibold text-gray-900">Analyze usage</h2>
        <p className="mt-1 text-[13px] text-gray-500">
          Samples recent request payloads and uses an LLM (via freeinference.org) to summarize{' '}
          <em>how</em> people use the gateway — which agents/harnesses, what tasks, and notable
          patterns. The API key stays in your browser and is sent only with this request.
        </p>

        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <label className="block text-[12px] font-medium text-gray-600 mb-1">
              freeinference.org API key
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              autoComplete="off"
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium text-gray-600 mb-1">Model</label>
            <input
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={DEFAULT_MODEL}
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium text-gray-600 mb-1">Sample size</label>
            <input
              type="number"
              min={MIN_LIMIT}
              max={MAX_LIMIT}
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              onBlur={() => setLimit(String(clampLimit(limit)))}
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>

          <div className="sm:col-span-2">
            <label className="block text-[12px] font-medium text-gray-600 mb-1">
              Scope to a user (optional)
            </label>
            <input
              type="email"
              value={userEmail}
              onChange={(e) => setUserEmail(e.target.value)}
              placeholder="leave blank to analyze all users"
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            />
          </div>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={analyze}
            disabled={loading}
            className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? 'Analyzing…' : 'Analyze usage'}
          </button>
          {result && !loading && (
            <span className="text-[12px] text-gray-400">
              {result.sampled_requests} request(s) · {result.model} · {result.scope}
            </span>
          )}
        </div>

        {error && (
          <div className="mt-4 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-600">{error}</div>
        )}
      </div>

      {loading && (
        <div className="rounded-xl border border-gray-100 bg-gray-50 p-5">
          <div className="mb-3 h-2.5 w-40 animate-pulse rounded bg-gray-200" />
          <div className="space-y-2">
            <div className="h-3 w-full animate-pulse rounded bg-gray-200" />
            <div className="h-3 w-11/12 animate-pulse rounded bg-gray-200" />
            <div className="h-3 w-4/5 animate-pulse rounded bg-gray-200" />
          </div>
        </div>
      )}

      {result && !loading && (
        <div className="rounded-xl border border-gray-200 bg-white p-6">
          <div className={MARKDOWN_CLASS}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.analysis}</ReactMarkdown>
          </div>
          <p className="mt-5 border-t border-gray-100 pt-3 text-[11px] text-gray-400">
            Generated {new Date(result.generated_at).toLocaleString()} · model {result.model} ·{' '}
            {result.sampled_requests} sampled request(s)
          </p>
        </div>
      )}
    </div>
  );
}
