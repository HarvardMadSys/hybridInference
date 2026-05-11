'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';

import type { RouteWeight } from '@/lib/api/admin';
import { clearRouteWeight, listRouteWeights, setRouteWeight } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

function rowKey(row: Pick<RouteWeight, 'model_id' | 'endpoint_id'>) {
  return `${row.model_id}\u0000${row.endpoint_id}`;
}

function formatWeight(value: number) {
  return String(value);
}

export function RoutingTab() {
  const [routes, setRoutes] = useState<RouteWeight[]>([]);
  const [draftWeights, setDraftWeights] = useState<Record<string, string>>({});
  const [savingKeys, setSavingKeys] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRoutes(await listRouteWeights());
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const groupedRoutes = useMemo(() => {
    const groups = new Map<string, RouteWeight[]>();
    for (const route of routes) {
      const group = groups.get(route.model_id) ?? [];
      group.push(route);
      groups.set(route.model_id, group);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [routes]);

  const replaceRoute = useCallback((updated: RouteWeight) => {
    setRoutes((prev) => prev.map((route) => (rowKey(route) === rowKey(updated) ? updated : route)));
  }, []);

  const markSaving = useCallback((key: string, saving: boolean) => {
    setSavingKeys((prev) => {
      const next = new Set(prev);
      if (saving) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  const handleSave = useCallback(
    async (route: RouteWeight) => {
      const key = rowKey(route);
      const draft = draftWeights[key] ?? formatWeight(route.effective_weight);
      const weight = Number(draft);
      if (!Number.isFinite(weight) || weight < 0) {
        toast.error('Weight must be a non-negative number.');
        return;
      }

      markSaving(key, true);
      try {
        const updated = await setRouteWeight(route.model_id, route.endpoint_id, weight);
        replaceRoute(updated);
        setDraftWeights((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
        toast.success(`Updated ${route.endpoint_id} weight.`);
      } catch (e) {
        toast.error(`Failed to update ${route.endpoint_id}: ${getErrorMessage(e)}`);
      } finally {
        markSaving(key, false);
      }
    },
    [draftWeights, markSaving, replaceRoute],
  );

  const handleClear = useCallback(
    async (route: RouteWeight) => {
      const key = rowKey(route);
      markSaving(key, true);
      try {
        const updated = await clearRouteWeight(route.model_id, route.endpoint_id);
        replaceRoute(updated);
        setDraftWeights((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
        toast.success(`Cleared ${route.endpoint_id} override.`);
      } catch (e) {
        toast.error(`Failed to clear ${route.endpoint_id}: ${getErrorMessage(e)}`);
      } finally {
        markSaving(key, false);
      }
    },
    [markSaving, replaceRoute],
  );

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-[14px] font-semibold text-gray-900">Routing Weights</h2>
        <p className="mt-1 text-sm text-gray-500">
          Override per-model endpoint weights at runtime. Clearing an override restores the YAML
          default.
        </p>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="rounded-xl border border-gray-200 bg-white py-12 text-center text-sm text-gray-400">
          Loading routing weights...
        </div>
      ) : groupedRoutes.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 bg-white px-4 py-8 text-center text-sm text-gray-500">
          No route weights available.
        </div>
      ) : (
        groupedRoutes.map(([modelId, modelRoutes]) => (
          <section
            key={modelId}
            className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm"
          >
            <h2 className="text-[15px] font-semibold text-gray-900">{modelId}</h2>
            <div className="mt-4 overflow-x-auto rounded-md border border-gray-200">
              <table className="min-w-full text-[13px]">
                <thead className="bg-gray-50 text-gray-500">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">Endpoint</th>
                    <th className="px-3 py-2 text-left font-medium">Provider</th>
                    <th className="px-3 py-2 text-left font-medium">YAML</th>
                    <th className="px-3 py-2 text-left font-medium">Override</th>
                    <th className="px-3 py-2 text-left font-medium">Effective</th>
                    <th className="px-3 py-2 text-right font-medium">Runtime Weight</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {modelRoutes.map((route) => {
                    const key = rowKey(route);
                    const isSaving = savingKeys.has(key);
                    const inputValue = draftWeights[key] ?? formatWeight(route.effective_weight);
                    return (
                      <tr key={key} className="bg-white align-top">
                        <td className="px-3 py-2 text-gray-900">
                          <div>{route.endpoint_id}</div>
                          {route.base_url && (
                            <div className="mt-1 max-w-[280px] truncate text-[11px] text-gray-400">
                              {route.base_url}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-600">{route.provider}</td>
                        <td className="px-3 py-2 text-gray-600">
                          {formatWeight(route.yaml_weight)}
                        </td>
                        <td className="px-3 py-2 text-gray-600">
                          {route.override_weight === null ? (
                            'Default'
                          ) : (
                            <span className="font-medium text-amber-700">Override active</span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-900">
                          {formatWeight(route.effective_weight)}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <div className="flex justify-end gap-2">
                            <input
                              aria-label={`Runtime weight for ${route.endpoint_id}`}
                              className="w-24 rounded-md border border-gray-300 px-2 py-1 text-right text-[13px] text-gray-900"
                              disabled={isSaving}
                              min={0}
                              step={0.1}
                              type="number"
                              value={inputValue}
                              onChange={(e) =>
                                setDraftWeights((prev) => ({ ...prev, [key]: e.target.value }))
                              }
                            />
                            <button
                              aria-label={`Save ${route.endpoint_id} weight`}
                              type="button"
                              disabled={isSaving}
                              onClick={() => void handleSave(route)}
                              className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-medium text-white disabled:opacity-50"
                            >
                              Save
                            </button>
                            {route.override_weight !== null && (
                              <button
                                aria-label={`Clear ${route.endpoint_id} override`}
                                type="button"
                                disabled={isSaving}
                                onClick={() => void handleClear(route)}
                                className="rounded-md border border-gray-300 px-2 py-1 text-[12px] font-medium text-gray-600 disabled:opacity-50"
                              >
                                Reset
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}
    </div>
  );
}
