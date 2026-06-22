'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import type { AdminModelConcurrencyItem } from '@/lib/api/admin';
import { listModelConcurrency, updateModelConcurrency } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

interface ModelConcurrencySectionProps {
  onToast: (msg: string) => void;
}

export function ModelConcurrencySection({ onToast }: ModelConcurrencySectionProps) {
  const queryClient = useQueryClient();
  const [models, setModels] = useState<AdminModelConcurrencyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingModelIds, setSavingModelIds] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listModelConcurrency();
      setModels(response.models);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleToggleExempt = useCallback(
    async (modelId: string, exempt: boolean) => {
      setSavingModelIds((prev) => new Set(prev).add(modelId));
      try {
        const updated = await updateModelConcurrency(modelId, exempt);
        setModels((prev) => prev.map((model) => (model.model_id === modelId ? updated : model)));
        void queryClient.invalidateQueries({ queryKey: ['user', 'models'] });
        onToast(`Updated concurrency exemption for ${modelId}.`);
      } catch (e) {
        onToast(`Failed to update ${modelId}: ${getErrorMessage(e)}`);
      } finally {
        setSavingModelIds((prev) => {
          const next = new Set(prev);
          next.delete(modelId);
          return next;
        });
      }
    },
    [onToast, queryClient],
  );

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">Model Concurrency Limit</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Exempt a model from the per-user concurrency limit. Requests to an exempt model do not
          count toward a user&apos;s normal in-flight request cap, but are still limited to at most
          64 concurrent requests per user.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
      ) : models.length === 0 ? (
        <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
          No models available.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-gray-200">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50 text-gray-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Model</th>
                <th className="px-3 py-2 text-right font-medium">Exempt</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const isSaving = savingModelIds.has(model.model_id);

                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-3 py-2 text-gray-900">{model.model_id}</td>
                    <td className="px-3 py-2 text-right">
                      <input
                        type="checkbox"
                        aria-label={`Concurrency exemption for ${model.model_id}`}
                        className="h-4 w-4 rounded border-gray-300"
                        disabled={isSaving}
                        checked={model.exempt}
                        onChange={(e) => void handleToggleExempt(model.model_id, e.target.checked)}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
