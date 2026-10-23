'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import type { AdminModelConcurrencyItem } from '@/lib/api/admin';
import { listModelConcurrency, updateModelConcurrency } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import { CHECKBOX, NOTICE_ACTION, TABLE_HEAD_CELL } from '@/components/features/admin/adminStyles';

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
    <AdminSection
      title="Model Concurrency Limit"
      description="Exempt a model from the per-user concurrency limit. Requests to an exempt model do not count toward a user's normal in-flight request cap, but are still limited to at most 64 concurrent requests per user."
    >
      {error && (
        <div className="mb-3">
          <AdminNotice
            action={
              <button type="button" onClick={() => void load()} className={NOTICE_ACTION}>
                Retry
              </button>
            }
          >
            {error}
          </AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : models.length === 0 ? (
        <AdminEmptyState title="No models available." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Model</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Exempt</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const isSaving = savingModelIds.has(model.model_id);

                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-gray-900">
                      {model.model_id}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <input
                        type="checkbox"
                        aria-label={`Concurrency exemption for ${model.model_id}`}
                        className={CHECKBOX}
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
    </AdminSection>
  );
}
