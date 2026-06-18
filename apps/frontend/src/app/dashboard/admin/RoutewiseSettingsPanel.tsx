'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';

import type { RoutewiseSettingItem } from '@/lib/api/admin';
import { listRoutewiseSettings, updateRoutewiseSetting } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { validateNumericSettingInput } from './numericSettingValidation';

function displayKey(setting: RoutewiseSettingItem) {
  return setting.key;
}

function validateSettingDraft(setting: RoutewiseSettingItem, draft: string) {
  if (setting.value_type === 'int' || setting.value_type === 'float') {
    return validateNumericSettingInput(draft, {
      min: setting.min,
      max: setting.max,
      integer: setting.value_type === 'int',
    });
  }
  return { ok: true as const, value: draft };
}

export function RoutewiseSettingsPanel() {
  const [settings, setSettings] = useState<RoutewiseSettingItem[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const loaded = await listRoutewiseSettings().then((resp) => resp.settings);
      setSettings(loaded);
      setDrafts(
        Object.fromEntries(loaded.map((setting) => [setting.key, String(setting.value ?? '')])),
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

  const handleSaveSetting = useCallback(
    async (setting: RoutewiseSettingItem) => {
      const draft = drafts[setting.key] ?? '';
      let value: string | number = draft;

      if (setting.value_type === 'int' || setting.value_type === 'float') {
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

      setSavingKey(setting.key);
      try {
        const updated = await updateRoutewiseSetting(setting.key, value);
        setSettings((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
        setDrafts((prev) => ({ ...prev, [updated.key]: String(updated.value ?? '') }));
        toast.success(`Updated ${displayKey(updated)}.`);
      } catch (e) {
        toast.error(`Failed to update ${displayKey(setting)}: ${getErrorMessage(e)}`);
      } finally {
        setSavingKey(null);
      }
    },
    [drafts],
  );

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">RouteWise parameters</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Runtime knobs used by RouteWise LP decisions for the selected routing policy.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-dashed border-gray-200 py-8 text-center text-sm text-gray-400">
          Loading RouteWise parameters...
        </div>
      ) : settings.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-200 py-8 text-center text-sm text-gray-500">
          No RouteWise parameters available.
        </div>
      ) : (
        <div className="space-y-3">
          {settings.map((setting) => {
            const draft = drafts[setting.key] ?? '';
            const isSaving = savingKey === setting.key;
            const validated = validateSettingDraft(setting, draft);
            const isDirty = validated.ok ? draft !== String(setting.value ?? '') : false;

            return (
              <div
                key={setting.key}
                className="grid gap-3 rounded-lg border border-gray-100 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_auto]"
              >
                <div className="min-w-0">
                  <div className="break-words text-[13px] font-medium text-gray-900">
                    {displayKey(setting)}
                  </div>
                  <p className="mt-0.5 text-[11px] leading-5 text-gray-500">
                    {setting.description}
                  </p>
                  {!validated.ok && draft !== '' && (
                    <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                      {validated.error}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 justify-self-start sm:justify-self-end">
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
                      setDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
                    }
                  />
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
      )}
    </section>
  );
}
