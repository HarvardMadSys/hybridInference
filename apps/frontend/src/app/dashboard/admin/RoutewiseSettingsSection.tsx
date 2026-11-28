'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import { NOTICE_ACTION } from '@/components/features/admin/adminStyles';
import { Select } from '@/components/ui/Select';
import type { ProviderRoute } from '@/lib/api/admin';
import { listProviderRoutes } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';
import { useReportUnsaved } from './useReportUnsaved';

// Why the model picker is unavailable while the panel holds a draft.
const HELD_FOR_DRAFTS = 'Save or discard the unsaved changes first';

interface RoutewiseSettingsSectionProps {
  /** Called with the number of unsaved changes whenever it changes. */
  onUnsavedChange?: (count: number) => void;
}

/** The routes of one model, grouped from the flat route list. */
function routesByModel(routes: ProviderRoute[]): Map<string, ProviderRoute[]> {
  const byModel = new Map<string, ProviderRoute[]>();
  for (const route of routes) {
    const existing = byModel.get(route.model_id);
    if (existing) existing.push(route);
    else byModel.set(route.model_id, [route]);
  }
  return byModel;
}

/** A model is RouteWise when its routes carry the RouteWise routing policy. */
function isRoutewiseModel(routes: ProviderRoute[]): boolean {
  return routes.some((route) => route.strategy === 'routewise');
}

/**
 * Admin → Settings → RouteWise: the RouteWise parameters and probes of one
 * model, split out of Admin → Routing. Only models whose routing policy is
 * RouteWise are offered; the routes, their limits and the policy stay on
 * Routing, and the RouteWise decisions stay there with them.
 */
export function RoutewiseSettingsSection({ onUnsavedChange }: RoutewiseSettingsSectionProps) {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Whether the panel holds an unsaved draft or a write in flight.
  const [editing, setEditing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listProviderRoutes();
      setRoutes(resp.routes);
      const byModel = routesByModel(resp.routes);
      const models = [...byModel.entries()]
        .filter(([, modelRoutes]) => isRoutewiseModel(modelRoutes))
        .map(([modelId]) => modelId)
        .sort();
      setSelectedModel((current) =>
        current && models.includes(current) ? current : (models[0] ?? ''),
      );
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useReportUnsaved(editing ? 1 : 0, onUnsavedChange);

  const byModel = useMemo(() => routesByModel(routes), [routes]);
  const models = useMemo(
    () =>
      [...byModel.entries()]
        .filter(([, modelRoutes]) => isRoutewiseModel(modelRoutes))
        .map(([modelId]) => modelId)
        .sort(),
    [byModel],
  );
  const selectedRoutes = useMemo(() => byModel.get(selectedModel) ?? [], [byModel, selectedModel]);

  const onEditingChange = useCallback((active: boolean) => setEditing(active), []);

  if (loading) return <AdminLoading size="sm" />;

  return (
    <div className="space-y-4">
      {error ? (
        <AdminNotice
          action={
            <button type="button" onClick={() => void load()} className={NOTICE_ACTION}>
              Retry
            </button>
          }
        >
          {error}
        </AdminNotice>
      ) : null}

      {!error && models.length === 0 ? (
        <AdminEmptyState title="No RouteWise models available.">
          A model appears here once its routing policy on Admin → Routing is RouteWise.
        </AdminEmptyState>
      ) : null}

      {models.length > 0 ? (
        <>
          <div>
            <label
              className="text-[12px] font-medium text-gray-500"
              htmlFor="routewise-settings-model"
            >
              Selected model
            </label>
            <Select
              id="routewise-settings-model"
              value={selectedModel}
              onValueChange={setSelectedModel}
              options={models.map((model) => ({ value: model, label: model }))}
              placeholder="Choose a model"
              searchable
              searchPlaceholder="Search models…"
              emptyText="No models match"
              // The panel's drafts belong to this model; save or discard them first.
              disabled={editing}
              title={editing ? HELD_FOR_DRAFTS : undefined}
              size="lg"
              className="mt-1 w-full min-w-[240px] max-w-sm"
            />
          </div>

          <RoutewiseSettingsPanel
            modelId={selectedModel}
            endpoints={selectedRoutes.map((route) => ({
              endpointId: route.endpoint_id,
              label: `${route.route_type} · ${route.upstream_provider || route.provider}`,
            }))}
            onEditingChange={onEditingChange}
          />
        </>
      ) : null}
    </div>
  );
}
