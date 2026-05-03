'use client';

import toast from 'react-hot-toast';
import { useAuth, hasRole } from '@/components/providers/AuthProvider';
import { useModels } from '@/lib/hooks';
import type { ModelCatalogItem } from '@/lib/api/user';

function formatTokenLimit(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`;
  return value.toLocaleString();
}

function formatPricing(model: ModelCatalogItem): string {
  const prompt = model.pricing.prompt;
  const completion = model.pricing.completion;
  if (!prompt && !completion) return '—';
  return `$${prompt ?? '0'} / $${completion ?? '0'} / 1M`;
}

function copyModelId(modelId: string): void {
  void navigator.clipboard.writeText(modelId);
  toast.success('Model ID copied to clipboard');
}

function isDashboardModelVisible(model: ModelCatalogItem, userRole: string | undefined): boolean {
  const p = model.owned_by.toLowerCase();
  if (p === 'codex_sub' || p === 'claude_sub') return false;
  // First-party OpenAI/Anthropic catalog entries are gated to internal+ users on the backend;
  // only hide them in the UI for users without that access.
  if (!hasRole(userRole, 'internal')) {
    if (p.includes('openai')) return false;
    if (p.includes('anthropic')) return false;
  }
  return true;
}

export function ModelsSection(): JSX.Element {
  const { data, isLoading, error } = useModels();
  const { state } = useAuth();
  const userRole = state.user?.role;
  const models = (data?.data ?? []).filter((m) => isDashboardModelVisible(m, userRole));

  return (
    <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200 sm:p-5">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold tracking-tight text-gray-900 sm:text-base">
            Models
          </h2>
          <p className="text-xs text-gray-500">Model IDs and token limits for API requests.</p>
        </div>
        {models.length > 0 && (
          <span className="text-xs text-gray-500">
            {models.length.toLocaleString()} model{models.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700 ring-1 ring-inset ring-red-200">
          Failed to load models. Please try again later.
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600" />
        </div>
      )}

      {!isLoading && !error && models.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-3 py-3 text-xs text-gray-600 ring-1 ring-inset ring-gray-200">
          No models are currently available for your account.
        </div>
      )}

      {!isLoading && !error && models.length > 0 && (
        <div className="overflow-x-auto -mx-1">
          <table className="w-full min-w-[22rem] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-gray-200 text-[11px] font-medium uppercase tracking-wide text-gray-500">
                <th className="py-1.5 pr-2">Model</th>
                <th className="py-1.5 pr-2 whitespace-nowrap">Ctx / out</th>
                <th className="hidden py-1.5 text-right sm:table-cell">$/1M</th>
              </tr>
            </thead>
            <tbody className="text-gray-800">
              {models.map((model) => (
                <tr key={model.id} className="border-b border-gray-100 last:border-0">
                  <td className="max-w-[14rem] py-1 pr-2 align-top">
                    <button
                      type="button"
                      onClick={() => copyModelId(model.id)}
                      className="block max-w-full truncate text-left font-mono text-[11px] text-blue-700 hover:text-blue-900"
                      title="Copy model ID"
                    >
                      {model.id}
                    </button>
                    <div className="truncate text-[11px] text-gray-500">{model.name}</div>
                  </td>
                  <td className="whitespace-nowrap py-1 pr-2 align-top tabular-nums text-gray-700">
                    {formatTokenLimit(model.context_length)} /{' '}
                    {formatTokenLimit(model.max_output_length)}
                  </td>
                  <td className="hidden py-1 align-top text-right text-[11px] text-gray-600 sm:table-cell">
                    {formatPricing(model)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
