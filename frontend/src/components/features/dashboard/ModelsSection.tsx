'use client';

import toast from 'react-hot-toast';
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
  if (!prompt && !completion) return 'No pricing';
  return `$${prompt ?? '0'} in / $${completion ?? '0'} out per 1M tokens`;
}

function copyModelId(modelId: string): void {
  void navigator.clipboard.writeText(modelId);
  toast.success('Model ID copied to clipboard');
}

function ModelCard({ model }: { model: ModelCatalogItem }): JSX.Element {
  const features = model.supported_features.filter((feature) => feature !== 'structured_outputs');
  const modalities = Array.from(new Set([...model.input_modalities, ...model.output_modalities]));

  return (
    <div className="rounded-lg bg-gray-50 p-4 ring-1 ring-inset ring-gray-200">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-gray-900">{model.name}</h3>
          <button
            type="button"
            onClick={() => copyModelId(model.id)}
            className="mt-1 block max-w-full truncate font-mono text-xs text-blue-700 hover:text-blue-900"
            title="Copy model ID"
          >
            {model.id}
          </button>
        </div>
        <span className="shrink-0 rounded-full bg-white px-2.5 py-1 text-xs font-medium capitalize text-gray-600 ring-1 ring-inset ring-gray-200">
          {model.owned_by}
        </span>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-xs text-gray-500">Context</div>
          <div className="font-medium text-gray-900">{formatTokenLimit(model.context_length)}</div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Max Output</div>
          <div className="font-medium text-gray-900">
            {formatTokenLimit(model.max_output_length)}
          </div>
        </div>
      </div>

      <div className="mt-3 text-xs text-gray-600">{formatPricing(model)}</div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <span className="rounded-md bg-white px-2 py-1 text-xs text-gray-600 ring-1 ring-inset ring-gray-200">
          {model.quantization}
        </span>
        {modalities.map((modality) => (
          <span
            key={`${model.id}-${modality}`}
            className="rounded-md bg-white px-2 py-1 text-xs text-gray-600 ring-1 ring-inset ring-gray-200"
          >
            {modality}
          </span>
        ))}
        {features.map((feature) => (
          <span
            key={`${model.id}-${feature}`}
            className="rounded-md bg-blue-50 px-2 py-1 text-xs text-blue-700 ring-1 ring-inset ring-blue-200"
          >
            {feature.replaceAll('_', ' ')}
          </span>
        ))}
      </div>
    </div>
  );
}

export function ModelsSection(): JSX.Element {
  const { data, isLoading, error } = useModels();
  const models = data?.data ?? [];

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold tracking-tight text-gray-900 sm:text-lg">
            Models
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            Available model IDs and limits for your FreeInference API requests.
          </p>
        </div>
        {models.length > 0 && (
          <span className="text-xs text-gray-500">
            {models.length.toLocaleString()} model{models.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-sm text-red-700 ring-1 ring-inset ring-red-200">
          Failed to load models. Please try again later.
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-8">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600" />
        </div>
      )}

      {!isLoading && !error && models.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-4 py-5 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
          No models are currently available for your account.
        </div>
      )}

      {!isLoading && !error && models.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {models.map((model) => (
            <ModelCard key={model.id} model={model} />
          ))}
        </div>
      )}
    </div>
  );
}
