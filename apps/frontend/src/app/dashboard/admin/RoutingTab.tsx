'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';

import type { RouteWeight } from '@/lib/api/admin';
import { clearRouteWeight, listRouteWeights, setRouteWeight } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { RouteWeightTable, rowKey } from './RouteWeightTable';

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

  const visibleRoutes = useMemo(
    () => routes.filter((route) => route.strategy !== 'routewise'),
    [routes],
  );

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
      ) : visibleRoutes.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 bg-white px-4 py-8 text-center text-sm text-gray-500">
          No route weights available.
        </div>
      ) : (
        <RouteWeightTable
          routes={visibleRoutes}
          draftWeights={draftWeights}
          savingKeys={savingKeys}
          onDraftChange={(key, value) => setDraftWeights((prev) => ({ ...prev, [key]: value }))}
          onSave={(route) => {
            void handleSave(route);
          }}
          onClear={(route) => {
            void handleClear(route);
          }}
        />
      )}
    </div>
  );
}
