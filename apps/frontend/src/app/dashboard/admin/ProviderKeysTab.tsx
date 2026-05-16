'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
  ProviderApiKeyItem,
  addProviderKey,
  deleteProviderKey,
  disableProviderEnvKey,
  getProviderQuotas,
  listProviderKeys,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

interface Props {
  refreshKey?: number;
}

function formatRelative(s: string | null): string {
  if (!s) return '—';
  const ms = Date.now() - new Date(s).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function ProviderKeysTab({ refreshKey = 0 }: Props) {
  const [providers, setProviders] = useState<string[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<string>('');
  const [keys, setKeys] = useState<ProviderApiKeyItem[]>([]);
  const [loading, setLoading] = useState(false);

  const [formProvider, setFormProvider] = useState<string>('');
  const [formApiKey, setFormApiKey] = useState('');
  const [formLabel, setFormLabel] = useState('');
  const [submittingKey, setSubmittingKey] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [disablingEnvId, setDisablingEnvId] = useState<string | null>(null);

  // Populate the provider dropdown from the existing provider-quotas
  // endpoint to avoid adding a new "list providers" endpoint.
  const loadProviders = useCallback(async () => {
    try {
      const resp = await getProviderQuotas();
      const names = Array.from(new Set(resp.providers.map((p) => p.name))).sort();
      setProviders(names);
      if (names.length > 0) {
        setSelectedProvider((current) => current || names[0]);
        setFormProvider((current) => current || names[0]);
      }
    } catch (err) {
      toast.error(`Failed to load providers: ${getErrorMessage(err)}`);
    }
  }, []);

  const loadKeys = useCallback(async (provider: string) => {
    if (!provider) {
      setKeys([]);
      return;
    }
    setLoading(true);
    try {
      const resp = await listProviderKeys(provider);
      setKeys(resp.keys);
    } catch (err) {
      toast.error(`Failed to load keys: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders, refreshKey]);

  useEffect(() => {
    void loadKeys(selectedProvider);
  }, [loadKeys, selectedProvider, refreshKey]);

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!formProvider || !formApiKey.trim()) return;
    setSubmittingKey(true);
    try {
      const resp = await addProviderKey(
        formProvider,
        formApiKey.trim(),
        formLabel.trim() || undefined,
      );
      toast.success(`Key added (${resp.pools_updated} pool(s) updated)`);
      setFormApiKey('');
      setFormLabel('');
      if (formProvider === selectedProvider) {
        await loadKeys(selectedProvider);
      } else {
        setSelectedProvider(formProvider);
      }
    } catch (err) {
      toast.error(`Add failed: ${getErrorMessage(err)}`);
    } finally {
      setSubmittingKey(false);
    }
  };

  const onDelete = async (id: string) => {
    if (!window.confirm('Delete this provider key? This cannot be undone.')) return;
    setDeletingId(id);
    try {
      await deleteProviderKey(id);
      toast.success('Key deleted');
      await loadKeys(selectedProvider);
    } catch (err) {
      toast.error(`Delete failed: ${getErrorMessage(err)}`);
    } finally {
      setDeletingId(null);
    }
  };

  const onDisableEnv = async (provider: string, id: string) => {
    if (!window.confirm('Disable this env provider key? It will stop being used immediately.')) {
      return;
    }
    setDisablingEnvId(id);
    try {
      await disableProviderEnvKey(provider, id);
      toast.success('Env key disabled');
      await loadKeys(selectedProvider);
    } catch (err) {
      toast.error(`Disable failed: ${getErrorMessage(err)}`);
    } finally {
      setDisablingEnvId(null);
    }
  };

  const sortedKeys = useMemo(
    () => [...keys].sort((a, b) => (a.source === b.source ? 0 : a.source === 'db' ? -1 : 1)),
    [keys],
  );

  return (
    <div className="space-y-6">
      <div>
        <label className="text-[12px] font-medium text-gray-500" htmlFor="provider-keys-select">
          Selected provider
        </label>
        <select
          id="provider-keys-select"
          value={selectedProvider}
          onChange={(e) => setSelectedProvider(e.target.value)}
          className="mt-1 w-full max-w-xs rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
        >
          {providers.length === 0 && <option value="">(no providers loaded)</option>}
          {providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <div>
        <h3 className="text-[14px] font-semibold text-gray-900">Configured keys</h3>
        {loading ? (
          <div className="flex justify-center py-12">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          </div>
        ) : sortedKeys.length === 0 ? (
          <p className="mt-3 text-[13px] text-gray-400">No keys configured for this provider.</p>
        ) : (
          <div className="mt-3 overflow-hidden rounded-lg border border-gray-200">
            <table className="w-full text-[13px]">
              <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2">Prefix</th>
                  <th className="px-3 py-2">Label</th>
                  <th className="px-3 py-2">Source</th>
                  <th className="px-3 py-2">Created</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {sortedKeys.map((k) => (
                  <tr key={`${k.source}-${k.id ?? k.key_prefix}`}>
                    <td className="px-3 py-2 font-mono text-[12px] text-gray-700">
                      {k.key_prefix}
                    </td>
                    <td className="px-3 py-2 text-gray-600">{k.label ?? '—'}</td>
                    <td className="px-3 py-2">
                      <span
                        className={
                          k.source === 'db'
                            ? 'rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-700'
                            : 'rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                        }
                      >
                        {k.source}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-500">{formatRelative(k.created_at)}</td>
                    <td className="px-3 py-2 text-right">
                      {k.source === 'env' ? (
                        <button
                          type="button"
                          onClick={() => k.id && onDisableEnv(k.provider, k.id)}
                          disabled={!k.id || disablingEnvId === k.id}
                          className="rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                        >
                          {disablingEnvId === k.id ? 'Disabling…' : 'Disable'}
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={() => k.id && onDelete(k.id)}
                          disabled={!k.id || deletingId === k.id}
                          className="rounded-md px-2 py-1 text-[12px] font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                        >
                          {deletingId === k.id ? 'Deleting…' : 'Delete'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="space-y-3 rounded-lg border border-gray-200 bg-white p-4">
        <h3 className="text-[14px] font-semibold text-gray-900">Add a new key</h3>
        <form aria-label="Add a new key" onSubmit={onSubmit} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="provider-keys-form-provider"
              >
                Provider
              </label>
              <select
                id="provider-keys-form-provider"
                value={formProvider}
                onChange={(e) => setFormProvider(e.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                required
              >
                {providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="provider-keys-form-label"
              >
                Label (optional)
              </label>
              <input
                id="provider-keys-form-label"
                type="text"
                value={formLabel}
                onChange={(e) => setFormLabel(e.target.value)}
                maxLength={255}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
              />
            </div>
          </div>
          <div>
            <label
              className="text-[12px] font-medium text-gray-500"
              htmlFor="provider-keys-form-key"
            >
              API key
            </label>
            <input
              id="provider-keys-form-key"
              type="password"
              value={formApiKey}
              onChange={(e) => setFormApiKey(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-mono focus:border-gray-400 focus:outline-none"
              required
            />
          </div>
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={submittingKey || !formProvider || !formApiKey.trim()}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submittingKey ? 'Adding…' : 'Add key'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default ProviderKeysTab;
