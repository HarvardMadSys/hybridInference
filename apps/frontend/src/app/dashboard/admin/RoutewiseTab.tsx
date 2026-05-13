'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';

import type { RouteWeight, RoutewiseSettingItem } from '@/lib/api/admin';
import {
  clearRouteWeight,
  listRouteWeights,
  listRoutewiseSettings,
  setRouteWeight,
  updateRoutewiseSetting,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { validateNumericSettingInput } from './numericSettingValidation';
import { RouteWeightTable, rowKey } from './RouteWeightTable';

function formatWeight(value: number) {
  return String(value);
}

function displayKey(setting: RoutewiseSettingItem) {
  if (setting.key === 'routewise_decision_rule' || setting.key === 'decision_rule') {
    return 'decision_rule';
  }
  return setting.key;
}

export function RoutewiseTab() {
  const [routes, setRoutes] = useState<RouteWeight[]>([]);
  const [routewiseSettings, setRoutewiseSettings] = useState<RoutewiseSettingItem[]>([]);
  const [draftWeights, setDraftWeights] = useState<Record<string, string>>({});
  const [settingDrafts, setSettingDrafts] = useState<Record<string, string>>({});
  const [savingKeys, setSavingKeys] = useState<Set<string>>(new Set());
  const [savingSettingKey, setSavingSettingKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [loadedRoutes, loadedSettings] = await Promise.all([
        listRouteWeights(),
        listRoutewiseSettings().then((resp) => resp.settings),
      ]);
      setRoutes(loadedRoutes);
      setRoutewiseSettings(loadedSettings);
      setSettingDrafts(
        Object.fromEntries(
          loadedSettings.map((setting) => [setting.key, String(setting.value ?? '')]),
        ),
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

  const visibleRoutes = useMemo(
    () => routes.filter((route) => route.strategy === 'routewise'),
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

  const handleSaveWeight = useCallback(
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

  const handleClearWeight = useCallback(
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

  const handleSaveSetting = useCallback(
    async (setting: RoutewiseSettingItem) => {
      const draft = settingDrafts[setting.key] ?? '';
      let value: string | number = draft;

      if (setting.key === 'routewise_decision_rule' || setting.key === 'decision_rule') {
        if (draft !== 'pd' && draft !== 'lapd') {
          toast.error(`${displayKey(setting)}: Select pd or lapd.`);
          return;
        }
      } else if (setting.value_type === 'int' || setting.value_type === 'float') {
        const validated = validateNumericSettingInput(draft, {
          min: setting.min,
          max: setting.max,
          integer: setting.value_type === 'int',
        });
        if (!validated.ok) {
          toast.error(`${displayKey(setting)}: ${validated.error}`);
          return;
        }
        value = validated.value;
      }

      setSavingSettingKey(setting.key);
      try {
        const updated = await updateRoutewiseSetting(setting.key, value);
        setRoutewiseSettings((prev) =>
          prev.map((item) => (item.key === updated.key ? updated : item)),
        );
        setSettingDrafts((prev) => ({ ...prev, [updated.key]: String(updated.value ?? '') }));
        toast.success(`Updated ${displayKey(updated)}.`);
      } catch (e) {
        toast.error(`Failed to update ${displayKey(setting)}: ${getErrorMessage(e)}`);
      } finally {
        setSavingSettingKey(null);
      }
    },
    [settingDrafts],
  );

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-[14px] font-semibold text-gray-900">Routewise Settings</h2>
        <p className="mt-1 text-sm text-gray-500">
          Configure Routewise runtime parameters and manage routewise-only endpoint weights.
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
          Loading routewise settings...
        </div>
      ) : (
        <>
          <div className="rounded-xl border border-gray-200 bg-white p-5">
            <div className="space-y-3">
              {routewiseSettings.map((setting) => {
                const draft = settingDrafts[setting.key] ?? '';
                const isSaving = savingSettingKey === setting.key;
                const isDecisionRule =
                  setting.key === 'routewise_decision_rule' || setting.key === 'decision_rule';
                const isNumericSetting =
                  setting.value_type === 'int' || setting.value_type === 'float';
                const validated = isNumericSetting
                  ? validateNumericSettingInput(draft, {
                    min: setting.min,
                    max: setting.max,
                    integer: setting.value_type === 'int',
                  })
                  : { ok: true as const, value: draft };
                const isDirty =
                  isDecisionRule || validated.ok ? draft !== String(setting.value ?? '') : false;

                return (
                  <div
                    key={setting.key}
                    className="flex items-center justify-between gap-4 rounded-lg border border-gray-100 px-4 py-3"
                  >
                    <div className="flex-1">
                      <div className="text-[13px] font-medium text-gray-900">
                        {displayKey(setting)}
                      </div>
                      <p className="mt-0.5 text-[11px] text-gray-500">{setting.description}</p>
                      {!isDecisionRule && !validated.ok && draft !== '' && (
                        <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                          {validated.error}
                        </p>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {isDecisionRule ? (
                        <select
                          aria-label={`${displayKey(setting)} value`}
                          className="rounded-md border border-gray-300 px-2 py-1 text-[13px] text-gray-900"
                          disabled={isSaving}
                          value={draft}
                          onChange={(e) =>
                            setSettingDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
                          }
                        >
                          <option value="pd">pd</option>
                          <option value="lapd">lapd</option>
                        </select>
                      ) : (
                        <input
                          aria-label={`${displayKey(setting)} value`}
                          className="w-28 rounded-md border border-gray-300 px-2 py-1 text-right text-[13px] text-gray-900"
                          disabled={isSaving}
                          max={setting.max ?? undefined}
                          min={setting.min ?? undefined}
                          step={setting.value_type === 'int' ? 1 : 'any'}
                          type="number"
                          value={draft}
                          onChange={(e) =>
                            setSettingDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
                          }
                        />
                      )}
                      <button
                        type="button"
                        aria-label={`Save ${displayKey(setting)}`}
                        disabled={!isDirty || isSaving}
                        onClick={() => {
                          void handleSaveSetting(setting);
                        }}
                        className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-medium text-white disabled:opacity-40"
                      >
                        Save
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {visibleRoutes.length === 0 ? (
            <div className="rounded-xl border border-dashed border-gray-200 bg-white px-4 py-8 text-center text-sm text-gray-500">
              No routewise route weights available.
            </div>
          ) : (
            <RouteWeightTable
              routes={visibleRoutes}
              draftWeights={draftWeights}
              savingKeys={savingKeys}
              onDraftChange={(key, value) => setDraftWeights((prev) => ({ ...prev, [key]: value }))}
              onSave={(route) => {
                void handleSaveWeight(route);
              }}
              onClear={(route) => {
                void handleClearWeight(route);
              }}
            />
          )}
        </>
      )}
    </div>
  );
}
