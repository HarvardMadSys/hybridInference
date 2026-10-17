'use client';

import toast from 'react-hot-toast';
import { useModels } from '@/lib/hooks';
import type { ModelCatalogItem } from '@/lib/api/user';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';

function formatTokenLimit(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`;
  return value.toLocaleString();
}

function ModelPricing({ pricing }: Pick<ModelCatalogItem, 'pricing'>): JSX.Element {
  const t = useT();
  const rates = [
    [t('dashboard.models.price_input', 'Input'), pricing.prompt],
    [t('dashboard.models.price_output', 'Output'), pricing.completion],
    [t('dashboard.models.price_cache_hit', 'Cache hit'), pricing.input_cache_reads],
    [t('dashboard.models.price_cache_write', 'Cache write'), pricing.input_cache_writes],
  ] as const;
  const hasPrice = (value: string | undefined): value is string =>
    value !== undefined && value.trim() !== '';

  if (!rates.some(([, value]) => hasPrice(value))) return <>—</>;

  return (
    <dl className="space-y-0.5 whitespace-nowrap tabular-nums">
      {rates.map(([label, value], index) => {
        if (index >= 2 && !hasPrice(value)) return null;
        // Zero cache rates use the ordinary input rate in gateway billing.
        const effectiveValue = index >= 2 && Number(value) === 0 ? pricing.prompt : value;
        return (
          <div key={label} className="flex justify-end gap-2">
            <dt className="text-gray-500">{label}</dt>
            <dd>{hasPrice(effectiveValue) ? `$${effectiveValue}` : '—'}</dd>
          </div>
        );
      })}
    </dl>
  );
}

export function isDashboardModelVisible(_model: ModelCatalogItem): boolean {
  return true;
}

export function ModelsSection(): JSX.Element {
  const { data, isLoading, error } = useModels();
  const models = (data?.data ?? []).filter(isDashboardModelVisible);
  const t = useT();
  const fmt = useFormatter();

  const copyModelId = (modelId: string): void => {
    void navigator.clipboard.writeText(modelId);
    toast.success(t('dashboard.models.copied_toast', 'Model ID copied to clipboard'));
  };

  return (
    <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200 sm:p-5">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold tracking-tight text-gray-900 sm:text-base">
            {t('dashboard.models.title', 'Models')}
          </h2>
          <p className="text-xs text-gray-500">
            {t('dashboard.models.subtitle', 'Model IDs and token limits for API requests.')}
          </p>
        </div>
        {models.length > 0 && (
          <span className="text-xs text-gray-500">
            {fill(
              models.length === 1
                ? t('dashboard.models.count_one', '{count} model')
                : t('dashboard.models.count_other', '{count} models'),
              { count: fmt.number(models.length) },
            )}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700 ring-1 ring-inset ring-red-200">
          {t('dashboard.models.load_failed', 'Failed to load models. Please try again later.')}
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600" />
        </div>
      )}

      {!isLoading && !error && models.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-3 py-3 text-xs text-gray-600 ring-1 ring-inset ring-gray-200">
          {t('dashboard.models.empty', 'No models are currently available for your account.')}
        </div>
      )}

      {!isLoading && !error && models.length > 0 && (
        <div className="overflow-x-auto -mx-1">
          <table className="w-full min-w-[22rem] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-gray-200 text-[11px] font-medium uppercase tracking-wide text-gray-500">
                <th className="py-1.5 pr-2">{t('dashboard.models.col_model', 'Model')}</th>
                <th className="py-1.5 pr-2 whitespace-nowrap">
                  {t('dashboard.models.col_limits', 'Ctx / out')}
                </th>
                <th className="py-1.5 text-right whitespace-nowrap">
                  {t('dashboard.models.col_price', 'USD / 1M tokens')}
                </th>
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
                      title={t('dashboard.models.copy', 'Copy model ID')}
                    >
                      {model.id}
                    </button>
                    <div className="truncate text-[11px] text-gray-500">{model.name}</div>
                  </td>
                  <td className="whitespace-nowrap py-1 pr-2 align-top tabular-nums text-gray-700">
                    {formatTokenLimit(model.context_length)} /{' '}
                    {formatTokenLimit(model.max_output_length)}
                  </td>
                  <td className="py-1 align-top text-right text-[11px] text-gray-600">
                    <ModelPricing pricing={model.pricing} />
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
