'use client';

import { useCallback, useEffect, useState } from 'react';

import type { AdminModelVisibilityItem, Role } from '@/lib/api/admin';
import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const DEFAULT_VALUE = '__default__';

const ROLE_OPTIONS: Array<{ value: Role; label: Role }> = [
  { value: 'free', label: 'free' },
  { value: 'pro', label: 'pro' },
  { value: 'internal', label: 'internal' },
  { value: 'admin', label: 'admin' },
];

interface ModelVisibilitySectionProps {
  onToast: (msg: string) => void;
}

export function ModelVisibilitySection({ onToast }: ModelVisibilitySectionProps) {
  const [models, setModels] = useState<AdminModelVisibilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingModelIds, setSavingModelIds] = useState<Set<string>>(new Set());
  const [draftOverrides, setDraftOverrides] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listModelVisibility();
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

  const handleChangeRole = useCallback(
    async (modelId: string, value: string) => {
      const requiredRole = value === DEFAULT_VALUE ? null : (value as Role);
      setDraftOverrides((prev) => ({ ...prev, [modelId]: value }));
      setSavingModelIds((prev) => new Set(prev).add(modelId));
      try {
        const updated = await updateModelVisibility(modelId, requiredRole);
        setModels((prev) => prev.map((model) => (model.model_id === modelId ? updated : model)));
        onToast(`Updated visibility for ${modelId}.`);
      } catch (e) {
        onToast(`Failed to update ${modelId}: ${getErrorMessage(e)}`);
      } finally {
        setDraftOverrides((prev) => {
          const next = { ...prev };
          delete next[modelId];
          return next;
        });
        setSavingModelIds((prev) => {
          const next = new Set(prev);
          next.delete(modelId);
          return next;
        });
      }
    },
    [onToast],
  );

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">Model Visibility</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Override each model&apos;s runtime required role without changing the YAML baseline.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}
          <button
            type="button"
            onClick={() => void load()}
            className="ml-2 font-medium underline"
          >
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
                <th className="px-3 py-2 text-left font-medium">Baseline</th>
                <th className="px-3 py-2 text-left font-medium">Override</th>
                <th className="px-3 py-2 text-left font-medium">Effective</th>
                <th className="px-3 py-2 text-right font-medium">Runtime Override</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const isSaving = savingModelIds.has(model.model_id);
                const selectedOverride =
                  draftOverrides[model.model_id] ?? (model.override_required_role ?? DEFAULT_VALUE);

                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-3 py-2 text-gray-900">{model.model_id}</td>
                    <td className="px-3 py-2 text-gray-500">{model.baseline_required_role}</td>
                    <td className="px-3 py-2 text-gray-500">
                      {model.override_required_role ?? 'Use default'}
                    </td>
                    <td className="px-3 py-2 text-gray-900">{model.effective_required_role}</td>
                    <td className="px-3 py-2 text-right">
                      <select
                        aria-label={`Runtime override for ${model.model_id}`}
                        className="rounded-md border border-gray-300 px-2 py-1 text-[13px] text-gray-900"
                        disabled={isSaving}
                        value={selectedOverride}
                        onChange={(e) => void handleChangeRole(model.model_id, e.target.value)}
                      >
                        <option value={DEFAULT_VALUE}>Use default</option>
                        {ROLE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
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
