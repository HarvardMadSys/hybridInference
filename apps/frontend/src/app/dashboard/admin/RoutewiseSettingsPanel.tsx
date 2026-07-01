'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';

import type { RoutewiseProbeSampleItem, RoutewiseSettingItem } from '@/lib/api/admin';
import {
  listRoutewiseProbeSamples,
  listRoutewiseSettings,
  runRoutewiseProbe,
  updateRoutewiseSetting,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { validateNumericSettingInput } from './numericSettingValidation';

const SETTING_LABELS: Record<string, string> = {
  routewise_budget_alpha: 'Cost budget alpha',
  routewise_latency_slo_sec: 'Latency SLO (sec)',
  routewise_latency_min_samples: 'Legacy sample threshold',
  routewise_probe_enabled: 'Background probes',
  routewise_probe_interval_sec: 'Probe interval (sec)',
};

type ProbeEndpointOption = {
  endpointId: string;
  label: string;
};

interface RoutewiseSettingsPanelProps {
  modelId?: string;
  endpoints?: ProbeEndpointOption[];
}

function displayKey(setting: RoutewiseSettingItem) {
  return SETTING_LABELS[setting.key] ?? setting.key;
}

function settingDraft(setting: RoutewiseSettingItem) {
  if (setting.value_type === 'bool') {
    return String(Boolean(setting.value));
  }
  return String(setting.value ?? '');
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

function formatTtft(ms: number | null) {
  if (ms === null || !Number.isFinite(ms)) return '—';
  return `${Math.round(ms).toLocaleString()} ms`;
}

function formatCheckedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function probeErrorMessage(error: string | null) {
  if (!error) return 'error';
  const text = error.trim();
  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === 'object') {
      const data = parsed as Record<string, unknown>;
      const nested = data.error && typeof data.error === 'object' ? data.error : data;
      const err = nested as Record<string, unknown>;
      const metadata = err.metadata && typeof err.metadata === 'object' ? err.metadata : null;
      const raw = metadata ? (metadata as Record<string, unknown>).raw : null;
      const message = raw || err.message || data.message;
      if (typeof message === 'string' && message.trim()) return shortenProbeError(message);
    }
  } catch {
    // Fall through to regex/plain-text extraction for truncated JSON.
  }

  const rawMatch = text.match(/"raw"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (rawMatch?.[1]) return shortenProbeError(rawMatch[1]);
  const messageMatch = text.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (messageMatch?.[1]) return shortenProbeError(messageMatch[1]);
  return shortenProbeError(text);
}

function shortenProbeError(error: string) {
  const cleaned = error
    .replace(/\\n/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/All (\d+) keys for provider '' are muted/g, 'All $1 keys are muted')
    .trim();
  if (cleaned.length <= 96) return cleaned;
  return `${cleaned.slice(0, 93).trimEnd()}...`;
}

function sampleStatusClass(sample: RoutewiseProbeSampleItem) {
  return sample.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700';
}

export function RoutewiseSettingsPanel({ modelId, endpoints = [] }: RoutewiseSettingsPanelProps) {
  const [settings, setSettings] = useState<RoutewiseSettingItem[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [probeEndpointId, setProbeEndpointId] = useState('');
  const [probeSamples, setProbeSamples] = useState<RoutewiseProbeSampleItem[]>([]);
  const [probeLoading, setProbeLoading] = useState(false);
  const [probeRunning, setProbeRunning] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [probeBanner, setProbeBanner] = useState<{
    kind: 'success' | 'error';
    text: string;
  } | null>(null);

  const probeEndpointOptions = useMemo(
    () => endpoints.filter((endpoint) => endpoint.endpointId),
    [endpoints],
  );
  const latestProbeSamples = useMemo(() => {
    const seen = new Set<string>();
    const latest: RoutewiseProbeSampleItem[] = [];
    const samples = Array.isArray(probeSamples) ? probeSamples : [];
    for (const sample of samples) {
      if (seen.has(sample.endpoint_id)) continue;
      seen.add(sample.endpoint_id);
      latest.push(sample);
    }
    return latest;
  }, [probeSamples]);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const loaded = await listRoutewiseSettings().then((resp) => resp.settings);
      setSettings(loaded);
      setDrafts(Object.fromEntries(loaded.map((setting) => [setting.key, settingDraft(setting)])));
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadProbeSamples = useCallback(async () => {
    if (!modelId) {
      setProbeSamples([]);
      return;
    }
    setProbeLoading(true);
    setProbeError(null);
    try {
      const loaded = await listRoutewiseProbeSamples({
        modelId,
        endpointId: probeEndpointId || undefined,
        sinceSeconds: 86_400,
        limit: 100,
      });
      setProbeSamples(loaded.samples);
    } catch (e) {
      setProbeError(getErrorMessage(e));
    } finally {
      setProbeLoading(false);
    }
  }, [modelId, probeEndpointId]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    void loadProbeSamples();
  }, [loadProbeSamples]);

  const handleSaveSetting = useCallback(
    async (setting: RoutewiseSettingItem) => {
      const draft = drafts[setting.key] ?? '';
      let value: string | number | boolean = draft;

      if (setting.value_type === 'bool') {
        value = draft === 'true';
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

      setSavingKey(setting.key);
      try {
        const updated = await updateRoutewiseSetting(setting.key, value);
        setSettings((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
        setDrafts((prev) => ({ ...prev, [updated.key]: settingDraft(updated) }));
        toast.success(`Updated ${displayKey(updated)}.`);
      } catch (e) {
        toast.error(`Failed to update ${displayKey(setting)}: ${getErrorMessage(e)}`);
      } finally {
        setSavingKey(null);
      }
    },
    [drafts],
  );

  const handleRunProbe = useCallback(async () => {
    if (!modelId) return;
    setProbeRunning(true);
    setProbeBanner(null);
    try {
      const response = await runRoutewiseProbe({
        model_id: modelId,
        endpoint_id: probeEndpointId || null,
        idle_only: false,
      });
      const failed = response.results.filter((result) => !result.ok);
      if (failed.length === 0) {
        const count = response.results.length;
        setProbeBanner({
          kind: 'success',
          text: count === 1 ? 'Probe succeeded' : `${count} probes succeeded`,
        });
        toast.success('RouteWise probe completed');
      } else {
        setProbeBanner({
          kind: 'error',
          text: `${failed.length} probe${failed.length === 1 ? '' : 's'} failed`,
        });
        toast.error('RouteWise probe finished with failures');
      }
      await loadProbeSamples();
    } catch (e) {
      const message = getErrorMessage(e);
      setProbeBanner({ kind: 'error', text: message });
      toast.error(`Probe failed: ${message}`);
    } finally {
      setProbeRunning(false);
    }
  }, [loadProbeSamples, modelId, probeEndpointId]);

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">RouteWise parameters</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          Runtime knobs and probe controls used by RouteWise latency/cost decisions.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
          <button
            type="button"
            onClick={() => void loadSettings()}
            className="ml-2 font-medium underline"
          >
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
            const isDirty = validated.ok ? draft !== settingDraft(setting) : false;
            const isBoolean = setting.value_type === 'bool';

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
                  {isBoolean ? (
                    <label className="flex h-8 items-center gap-2 rounded-md border border-gray-200 px-2 text-[12px] font-medium text-gray-700">
                      <input
                        aria-label={`${displayKey(setting)} enabled`}
                        className="h-4 w-4 rounded border-gray-300 text-gray-900"
                        disabled={isSaving}
                        type="checkbox"
                        checked={draft === 'true'}
                        onChange={(e) =>
                          setDrafts((prev) => ({
                            ...prev,
                            [setting.key]: String(e.target.checked),
                          }))
                        }
                      />
                      Enabled
                    </label>
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
                        setDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
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
      )}

      <div className="mt-5 border-t border-gray-100 pt-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h3 className="text-[13px] font-semibold text-gray-900">Active probes</h3>
            <p className="mt-1 text-[12px] text-gray-500">
              Warm RouteWise TTFT state for this model without waiting for user traffic.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label
                className="text-[11px] font-medium text-gray-500"
                htmlFor="routewise-probe-endpoint"
              >
                Endpoint
              </label>
              <select
                id="routewise-probe-endpoint"
                value={probeEndpointId}
                onChange={(event) => setProbeEndpointId(event.target.value)}
                className="mt-1 h-9 min-w-[220px] rounded-md border border-gray-200 bg-white px-2 text-[12px] text-gray-800"
              >
                <option value="">All endpoints</option>
                {probeEndpointOptions.map((endpoint) => (
                  <option key={endpoint.endpointId} value={endpoint.endpointId}>
                    {endpoint.label}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              disabled={!modelId || probeRunning}
              onClick={() => void handleRunProbe()}
              className={
                probeBanner?.kind === 'success'
                  ? 'h-9 rounded-md bg-emerald-600 px-3 text-[12px] font-medium text-white shadow-sm shadow-emerald-100 disabled:opacity-50'
                  : 'h-9 rounded-md bg-gray-900 px-3 text-[12px] font-medium text-white disabled:opacity-50'
              }
            >
              {probeRunning ? 'Probing...' : 'Run probe'}
            </button>
            <button
              type="button"
              disabled={!modelId || probeLoading}
              onClick={() => void loadProbeSamples()}
              className="h-9 rounded-md border border-gray-200 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
            >
              Refresh
            </button>
          </div>
        </div>

        {probeBanner && (
          <div
            className={
              probeBanner.kind === 'success'
                ? 'mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-[12px] font-medium text-emerald-700'
                : 'mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] font-medium text-red-700'
            }
            role="status"
          >
            {probeBanner.text}
          </div>
        )}

        {probeError && (
          <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700">
            {probeError}
          </div>
        )}

        <div className="mt-3 overflow-hidden rounded-lg border border-gray-100">
          <div className="grid grid-cols-[minmax(0,1.3fr)_minmax(120px,.8fr)_90px_minmax(126px,.8fr)] gap-3 bg-gray-50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
            <div>Endpoint</div>
            <div>Status</div>
            <div>TTFT</div>
            <div>Checked</div>
          </div>
          {probeLoading ? (
            <div className="px-3 py-6 text-center text-[12px] text-gray-400">
              Loading probe samples...
            </div>
          ) : latestProbeSamples.length === 0 ? (
            <div className="px-3 py-6 text-center text-[12px] text-gray-400">
              No probe samples in the last 24 hours.
            </div>
          ) : (
            <div className="divide-y divide-gray-100">
              {latestProbeSamples.map((sample, index) => (
                <div
                  key={`${sample.endpoint_id}-${sample.checked_at}-${index}`}
                  className="grid grid-cols-[minmax(0,1.3fr)_minmax(120px,.8fr)_90px_minmax(126px,.8fr)] gap-3 px-3 py-2 text-[12px]"
                >
                  <div className="min-w-0 break-all font-mono text-gray-600">
                    {sample.endpoint_id}
                  </div>
                  <div className="min-w-0">
                    <span
                      title={sample.ok ? undefined : sample.error || undefined}
                      className={`inline-flex max-w-full rounded px-1.5 py-0.5 text-[11px] font-medium ${sampleStatusClass(
                        sample,
                      )}`}
                    >
                      <span className="break-words">
                        {sample.ok ? 'ok' : probeErrorMessage(sample.error)}
                      </span>
                    </span>
                  </div>
                  <div className="text-gray-700">{formatTtft(sample.ttft_ms)}</div>
                  <div className="text-gray-500">{formatCheckedAt(sample.checked_at)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
