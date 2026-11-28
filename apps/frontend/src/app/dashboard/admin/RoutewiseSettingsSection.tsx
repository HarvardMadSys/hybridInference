'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import { NOTICE_ACTION } from '@/components/features/admin/adminStyles';
import { Select } from '@/components/ui/Select';
import type { OpenRouterProviderOption, ProviderRoute, ProviderRouteOption } from '@/lib/api/admin';
import { listProviderRoutes } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { routeTargetLabel } from './routeTargetLabel';
import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';
import { useReportUnsaved } from './useReportUnsaved';

// Why the model picker is unavailable while the panel holds a draft.
const HELD_FOR_DRAFTS = 'Save or discard the unsaved changes first';

interface RoutewiseSettingsSectionProps {
  /** Called with the number of unsaved changes whenever it changes. */
  onUnsavedChange?: (count: number) => void;
  /**
   * Whether this section's sub-tab is the one on screen. Settings keeps every
   * sub-tab mounted, so without this the routes, settings and probe samples
   * would load on every Settings visit, opened or not. Defaults to shown.
   */
  active?: boolean;
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

/** The RouteWise models in `byModel`, sorted: a model is RouteWise when its routes carry that policy. */
function routewiseModels(byModel: Map<string, ProviderRoute[]>): string[] {
  return [...byModel.entries()]
    .filter(([, routes]) => routes.some((route) => route.strategy === 'routewise'))
    .map(([modelId]) => modelId)
    .sort();
}

/**
 * Admin → Settings → Providers & Routing: the RouteWise parameters and probes of one
 * model, split out of Admin → Routing. Only models whose routing policy is
 * RouteWise are offered; the routes, their limits and the policy stay on
 * Routing, and the RouteWise decisions stay there with them.
 *
 * Nothing loads until the sub-tab is first shown.
 */
export function RoutewiseSettingsSection({
  onUnsavedChange,
  active = true,
}: RoutewiseSettingsSectionProps) {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [providerOptions, setProviderOptions] = useState<ProviderRouteOption[]>([]);
  const [openRouterOptions, setOpenRouterOptions] = useState<OpenRouterProviderOption[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Whether the panel holds an unsaved draft or a write in flight.
  const [editing, setEditing] = useState(false);
  const [started, setStarted] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listProviderRoutes();
      setRoutes(resp.routes);
      setProviderOptions(resp.provider_options);
      setOpenRouterOptions(resp.openrouter_provider_options ?? []);
      const models = routewiseModels(routesByModel(resp.routes));
      setSelectedModel((current) =>
        current && models.includes(current) ? current : (models[0] ?? ''),
      );
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Load once, the first time the sub-tab is shown.
  useEffect(() => {
    if (!active || started) return;
    setStarted(true);
    void load();
  }, [active, load, started]);

  useReportUnsaved(editing ? 1 : 0, onUnsavedChange);

  const byModel = useMemo(() => routesByModel(routes), [routes]);
  const models = useMemo(() => routewiseModels(byModel), [byModel]);
  const selectedRoutes = useMemo(() => byModel.get(selectedModel) ?? [], [byModel, selectedModel]);

  const onEditingChange = useCallback((activePanel: boolean) => setEditing(activePanel), []);

  if (!started) return null;
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
              label: `${route.route_type} · ${routeTargetLabel(route, providerOptions, openRouterOptions)}`,
            }))}
            onEditingChange={onEditingChange}
          />
        </>
      ) : null}
    </div>
  );
}
