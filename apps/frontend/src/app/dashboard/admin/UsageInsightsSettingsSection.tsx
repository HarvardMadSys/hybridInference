'use client';

import { useCallback, useEffect, useState } from 'react';

import {
  UsageInsightsSettings,
  getUsageInsightsSettings,
  updateUsageInsightsSettings,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

interface UsageInsightsSettingsSectionProps {
  onToast: (msg: string) => void;
}

/**
 * Configures the provider used by the admin Usage Insights analysis: a
 * freeinference.org API key (stored server-side, never returned in full) and the
 * model to run the report with. The Analyze action lives on the Usage Insights
 * tab and on each user's detail panel; this section only manages credentials.
 */
export function UsageInsightsSettingsSection({ onToast }: UsageInsightsSettingsSectionProps) {
  const [settings, setSettings] = useState<UsageInsightsSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Empty key field means "leave the stored key unchanged" on save.
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await getUsageInsightsSettings();
      setSettings(s);
      setModel(s.model);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onSave = async () => {
    const trimmedModel = model.trim();
    if (!trimmedModel) {
      onToast('Model name cannot be empty');
      return;
    }
    setBusy(true);
    try {
      const patch: { api_key?: string; model?: string } = {};
      if (apiKey.trim()) patch.api_key = apiKey.trim();
      if (trimmedModel !== settings?.model) patch.model = trimmedModel;
      if (patch.api_key === undefined && patch.model === undefined) {
        onToast('Nothing to save');
        setBusy(false);
        return;
      }
      const updated = await updateUsageInsightsSettings(patch);
      setSettings(updated);
      setModel(updated.model);
      setApiKey('');
      onToast('Usage Insights provider saved');
    } catch (e) {
      onToast(`Failed to save: ${getErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const onClearKey = async () => {
    setBusy(true);
    try {
      const updated = await updateUsageInsightsSettings({ api_key: '' });
      setSettings(updated);
      setApiKey('');
      onToast('API key cleared');
    } catch (e) {
      onToast(`Failed to clear key: ${getErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const configured = !!settings?.configured;

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
      <div className="mb-3">
        <h2 className="text-[14px] font-semibold text-gray-900">Usage Insights</h2>
        <p className="mt-1 text-[12px] text-gray-500">
          API key and model for the LLM-powered Usage Insights analysis (via freeinference.org). The
          key is stored server-side and never shown again. Run the analysis from the Usage Insights
          tab or from a user&apos;s detail panel.
        </p>
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}{' '}
          <button onClick={() => setError(null)} className="ml-2 font-bold">
            &times;
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                configured ? 'bg-emerald-500' : 'bg-gray-300'
              }`}
            />
            <span className="text-[13px] text-gray-900">
              {configured ? (
                <>
                  Key configured{' '}
                  <span className="font-mono text-gray-500">{settings?.api_key_hint}</span>
                </>
              ) : (
                'No API key configured'
              )}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label className="mb-1 block text-[12px] font-medium text-gray-600">
                freeinference.org API key
              </label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={configured ? 'Enter a new key to replace the stored one' : 'sk-...'}
                autoComplete="off"
                className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              />
            </div>
            <div>
              <label className="mb-1 block text-[12px] font-medium text-gray-600">Model</label>
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="glm-5.1"
                className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              />
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onSave}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            {configured && (
              <button
                type="button"
                disabled={busy}
                onClick={onClearKey}
                className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-40"
              >
                Clear key
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
