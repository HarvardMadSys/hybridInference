'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import type {
  ProviderDefinitionItem,
  ProbeProviderDefinitionResponse,
  UpdateProviderDefinitionPayload,
} from '@/lib/api/admin';
import {
  createProviderDefinition,
  deleteProviderDefinition,
  listProviderDefinitions,
  probeProviderDefinition,
  updateProviderDefinition,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
}

function formatBaseUrl(value: string): string {
  if (!value) return '—';
  try {
    const url = new URL(value.includes('://') ? value : `https://${value}`);
    return `${url.host}${url.pathname === '/' ? '' : url.pathname}`;
  } catch {
    return value.replace(/^https?:\/\//, '');
  }
}

function formatMs(value: number | null): string {
  if (value == null) return '—';
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

interface AddProviderModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => Promise<void>;
}

function AddProviderModal({ open, onClose, onCreated }: AddProviderModalProps) {
  const [displayName, setDisplayName] = useState('');
  const [provider, setProvider] = useState('');
  const [providerTouched, setProviderTouched] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiKeyLabel, setApiKeyLabel] = useState('');
  const [probeModelId, setProbeModelId] = useState('');
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeProviderDefinitionResponse | null>(null);
  const [verifiedSignature, setVerifiedSignature] = useState<string | null>(null);

  const signature = useMemo(
    () => JSON.stringify([baseUrl.trim(), apiKey.trim(), probeModelId.trim()]),
    [baseUrl, apiKey, probeModelId],
  );
  const probeCurrent = probeResult != null && verifiedSignature === signature;
  const canProbe = baseUrl.trim() && apiKey.trim() && probeModelId.trim();
  const canSave =
    probeCurrent && displayName.trim() && provider.trim() && canProbe && !probing && !saving;

  const resetProbe = () => {
    setProbeResult(null);
    setVerifiedSignature(null);
  };

  const resetForm = useCallback(() => {
    setDisplayName('');
    setProvider('');
    setProviderTouched(false);
    setBaseUrl('');
    setApiKey('');
    setApiKeyLabel('');
    setProbeModelId('');
    resetProbe();
  }, []);

  useEffect(() => {
    if (!open) resetForm();
  }, [open, resetForm]);

  if (!open) return null;

  const onDisplayNameChange = (value: string) => {
    setDisplayName(value);
    if (!providerTouched) {
      setProvider(slugify(value));
    }
  };

  const onProbe = async () => {
    if (!canProbe) return;
    setProbing(true);
    resetProbe();
    try {
      const result = await probeProviderDefinition({
        default_base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        probe_model_id: probeModelId.trim(),
      });
      setProbeResult(result);
      setVerifiedSignature(signature);
      toast.success('Provider probe succeeded');
    } catch (err) {
      toast.error(`Probe failed: ${getErrorMessage(err)}`);
    } finally {
      setProbing(false);
    }
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      await createProviderDefinition({
        provider: provider.trim(),
        display_name: displayName.trim(),
        adapter_kind: 'openai_compat',
        default_base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        api_key_label: apiKeyLabel.trim() || null,
        probe_model_id: probeModelId.trim(),
      });
      toast.success('Provider added');
      await onCreated();
      onClose();
    } catch (err) {
      toast.error(`Add provider failed: ${getErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
      <div className="w-full max-w-2xl rounded-lg bg-white shadow-xl">
        <form onSubmit={onSubmit}>
          <div className="border-b border-gray-100 px-5 py-4">
            <h3 className="text-[16px] font-semibold text-gray-900">Add provider</h3>
          </div>
          <div className="space-y-4 px-5 py-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-name"
                >
                  Display name
                </label>
                <input
                  id="provider-name"
                  value={displayName}
                  onChange={(event) => onDisplayNameChange(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-slug"
                >
                  Slug
                </label>
                <input
                  id="provider-slug"
                  value={provider}
                  onChange={(event) => {
                    setProviderTouched(true);
                    setProvider(slugify(event.target.value));
                  }}
                  pattern="[a-z0-9][a-z0-9_-]{0,63}"
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-adapter"
                >
                  Adapter
                </label>
                <input
                  id="provider-adapter"
                  value="OpenAI-compatible"
                  disabled
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-[13px] text-gray-500"
                />
              </div>
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="probe-model-id"
                >
                  Probe model ID
                </label>
                <input
                  id="probe-model-id"
                  value={probeModelId}
                  onChange={(event) => {
                    setProbeModelId(event.target.value);
                    resetProbe();
                  }}
                  placeholder="minimax-m2.5"
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
            </div>

            <div>
              <label
                className="block text-[12px] font-medium text-gray-500"
                htmlFor="provider-base-url"
              >
                Base URL
              </label>
              <input
                id="provider-base-url"
                value={baseUrl}
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  resetProbe();
                }}
                placeholder="https://api.lkeap.cloud.tencent.com/plan/v3"
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-api-key"
                >
                  API key
                </label>
                <input
                  id="provider-api-key"
                  type="password"
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                    resetProbe();
                  }}
                  autoComplete="off"
                  spellCheck={false}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-key-label"
                >
                  Key label
                </label>
                <input
                  id="provider-key-label"
                  value={apiKeyLabel}
                  onChange={(event) => setApiKeyLabel(event.target.value)}
                  placeholder="Initial key"
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                />
              </div>
            </div>

            {probeCurrent && probeResult && (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
                <div className="font-medium">Probe successful</div>
                <div className="mt-1 grid gap-1 sm:grid-cols-2">
                  <span>First event TTFT: {formatMs(probeResult.first_event_ttft_ms)}</span>
                  <span>First content TTFT: {formatMs(probeResult.first_content_ttft_ms)}</span>
                </div>
                {probeResult.preview && (
                  <div className="mt-1 truncate text-emerald-700">{probeResult.preview}</div>
                )}
              </div>
            )}
          </div>
          <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-4">
            <button
              type="button"
              onClick={onClose}
              disabled={probing || saving}
              className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onProbe}
              disabled={probing || saving || !canProbe}
              className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {probing ? 'Probing…' : probeCurrent ? 'Probe again' : 'Probe'}
            </button>
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {saving ? 'Saving…' : 'Save provider'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface EditProviderModalProps {
  provider: ProviderDefinitionItem | null;
  onClose: () => void;
  onUpdated: () => Promise<void>;
}

function EditProviderModal({ provider, onClose, onUpdated }: EditProviderModalProps) {
  const [displayName, setDisplayName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [probeModelId, setProbeModelId] = useState('');
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeProviderDefinitionResponse | null>(null);
  const [verifiedSignature, setVerifiedSignature] = useState<string | null>(null);

  useEffect(() => {
    setDisplayName(provider?.display_name ?? '');
    setBaseUrl(provider?.default_base_url ?? '');
    setApiKey('');
    setProbeModelId('');
    setProbeResult(null);
    setVerifiedSignature(null);
  }, [provider]);

  const resetProbe = () => {
    setProbeResult(null);
    setVerifiedSignature(null);
  };

  const signature = useMemo(
    () => JSON.stringify([baseUrl.trim(), apiKey.trim(), probeModelId.trim()]),
    [baseUrl, apiKey, probeModelId],
  );
  const customProvider = provider?.source === 'custom';
  const baseUrlChanged = provider != null && baseUrl.trim() !== provider.default_base_url;
  const baseUrlNeedsProbe = baseUrlChanged && customProvider;
  const probeCurrent = probeResult != null && verifiedSignature === signature;
  const canProbe = baseUrlNeedsProbe && baseUrl.trim() && apiKey.trim() && probeModelId.trim();
  const canSave =
    provider != null &&
    displayName.trim() &&
    baseUrl.trim() &&
    !probing &&
    !saving &&
    (!baseUrlNeedsProbe || probeCurrent);

  if (!provider) return null;

  const onProbe = async () => {
    if (!canProbe) return;
    setProbing(true);
    resetProbe();
    try {
      const result = await probeProviderDefinition({
        default_base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        probe_model_id: probeModelId.trim(),
      });
      setProbeResult(result);
      setVerifiedSignature(signature);
      toast.success('Provider probe succeeded');
    } catch (err) {
      toast.error(`Probe failed: ${getErrorMessage(err)}`);
    } finally {
      setProbing(false);
    }
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const payload: UpdateProviderDefinitionPayload = {
        display_name: displayName.trim(),
      };
      if (baseUrlChanged) {
        payload.default_base_url = baseUrl.trim();
        if (baseUrlNeedsProbe) {
          payload.api_key = apiKey.trim();
          payload.probe_model_id = probeModelId.trim();
        }
      }
      await updateProviderDefinition(provider.provider, payload);
      toast.success('Provider updated');
      await onUpdated();
      onClose();
    } catch (err) {
      toast.error(`Update failed: ${getErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
      <div className="w-full max-w-2xl rounded-lg bg-white shadow-xl">
        <form onSubmit={onSubmit}>
          <div className="border-b border-gray-100 px-5 py-4">
            <h3 className="text-[16px] font-semibold text-gray-900">Edit provider</h3>
          </div>
          <div className="space-y-4 px-5 py-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="edit-provider-name"
                >
                  Display name
                </label>
                <input
                  id="edit-provider-name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="edit-provider-slug"
                >
                  Slug
                </label>
                <input
                  id="edit-provider-slug"
                  value={provider.provider}
                  disabled
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-[13px] text-gray-500"
                />
              </div>
            </div>

            <div>
              <label
                className="block text-[12px] font-medium text-gray-500"
                htmlFor="edit-provider-base-url"
              >
                Base URL
              </label>
              <input
                id="edit-provider-base-url"
                value={baseUrl}
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  resetProbe();
                }}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>

            {baseUrlNeedsProbe && (
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label
                    className="block text-[12px] font-medium text-gray-500"
                    htmlFor="edit-provider-api-key"
                  >
                    API key
                  </label>
                  <input
                    id="edit-provider-api-key"
                    type="password"
                    value={apiKey}
                    onChange={(event) => {
                      setApiKey(event.target.value);
                      resetProbe();
                    }}
                    autoComplete="off"
                    spellCheck={false}
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                    required
                  />
                </div>
                <div>
                  <label
                    className="block text-[12px] font-medium text-gray-500"
                    htmlFor="edit-probe-model-id"
                  >
                    Probe model ID
                  </label>
                  <input
                    id="edit-probe-model-id"
                    value={probeModelId}
                    onChange={(event) => {
                      setProbeModelId(event.target.value);
                      resetProbe();
                    }}
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                    required
                  />
                </div>
              </div>
            )}

            {probeCurrent && probeResult && (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
                <div className="font-medium">Probe successful</div>
                <div className="mt-1 grid gap-1 sm:grid-cols-2">
                  <span>First event TTFT: {formatMs(probeResult.first_event_ttft_ms)}</span>
                  <span>First content TTFT: {formatMs(probeResult.first_content_ttft_ms)}</span>
                </div>
                {probeResult.preview && (
                  <div className="mt-1 truncate text-emerald-700">{probeResult.preview}</div>
                )}
              </div>
            )}
          </div>
          <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-4">
            <button
              type="button"
              onClick={onClose}
              disabled={probing || saving}
              className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Cancel
            </button>
            {baseUrlNeedsProbe && (
              <button
                type="button"
                onClick={onProbe}
                disabled={probing || saving || !canProbe}
                className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {probing ? 'Probing…' : probeCurrent ? 'Probe again' : 'Probe'}
              </button>
            )}
            <button
              type="submit"
              disabled={!canSave}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface DeleteProviderModalProps {
  provider: ProviderDefinitionItem | null;
  onClose: () => void;
  onDeleted: () => Promise<void>;
}

function DeleteProviderModal({ provider, onClose, onDeleted }: DeleteProviderModalProps) {
  const [confirmValue, setConfirmValue] = useState('');
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    setConfirmValue('');
  }, [provider?.provider]);

  if (!provider) return null;

  const canDelete = confirmValue === provider.provider && !deleting;
  const customProvider = provider.source === 'custom';

  const onDelete = async () => {
    if (!canDelete) return;
    setDeleting(true);
    try {
      await deleteProviderDefinition(provider.provider);
      toast.success('Provider deleted');
      await onDeleted();
      onClose();
    } catch (err) {
      toast.error(`Delete failed: ${getErrorMessage(err)}`);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
      <div className="w-full max-w-md rounded-lg bg-white shadow-xl">
        <div className="border-b border-gray-100 px-5 py-4">
          <h3 className="text-[16px] font-semibold text-gray-900">
            Delete provider “{provider.display_name}”
          </h3>
        </div>
        <div className="space-y-3 px-5 py-4">
          <p className="text-[13px] text-gray-600">
            {customProvider
              ? 'This removes the custom provider definition and its stored provider keys. This cannot be undone.'
              : 'This removes the provider from the admin registry. Existing config files are not modified.'}
          </p>
          <div>
            <label
              className="block text-[12px] font-medium text-gray-500"
              htmlFor="delete-provider-confirm"
            >
              Type {provider.provider} to confirm
            </label>
            <input
              id="delete-provider-confirm"
              value={confirmValue}
              onChange={(event) => setConfirmValue(event.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
            />
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onDelete}
            disabled={!canDelete}
            className="rounded-md bg-red-600 px-4 py-2 text-[13px] font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {deleting ? 'Deleting…' : 'Delete provider'}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ProviderOverviewTab() {
  const [providers, setProviders] = useState<ProviderDefinitionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<ProviderDefinitionItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProviderDefinitionItem | null>(null);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await listProviderDefinitions();
      setProviders(resp.providers);
    } catch (err) {
      toast.error(`Failed to load providers: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-[14px] font-semibold text-gray-900">Provider registry</h3>
          <p className="mt-1 text-[12px] text-gray-500">
            Built-in and custom upstream providers available to keys and routing.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          className="rounded-lg bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700"
        >
          Add Provider
        </button>
      </div>

      {loading && providers.length === 0 ? (
        <div className="flex justify-center py-24">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : providers.length === 0 ? (
        <div className="py-24 text-center">
          <p className="text-[13px] text-gray-400">No provider data.</p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-200">
          <table className="w-full min-w-[760px] text-[13px]">
            <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Provider</th>
                <th className="px-3 py-2">Base URL</th>
                <th className="px-3 py-2 text-right">Keys</th>
                <th className="px-3 py-2 text-right">Models</th>
                <th className="px-3 py-2 text-right">Manage</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {providers.map((provider) => {
                const custom = provider.source === 'custom';
                const inUse = provider.models_count > 0;
                return (
                  <tr key={provider.provider}>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-900">{provider.display_name}</span>
                        {custom && (
                          <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-700">
                            Custom
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-mono text-[12px] text-gray-400">
                        {provider.provider}
                      </div>
                    </td>
                    <td
                      className="max-w-[320px] truncate px-3 py-3 font-mono text-[12px] text-gray-600"
                      title={provider.default_base_url || undefined}
                    >
                      {formatBaseUrl(provider.default_base_url)}
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums text-gray-700">
                      {provider.keys_count}
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums text-gray-700">
                      {provider.models_count}
                    </td>
                    <td className="px-3 py-3 text-right">
                      <div className="flex justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => setEditTarget(provider)}
                          className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-700 hover:bg-gray-100"
                        >
                          Edit
                        </button>
                        {inUse ? (
                          <span className="px-2 py-1 text-[12px] text-gray-400">In use</span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setDeleteTarget(provider)}
                            className="rounded-md px-2 py-1 text-[12px] font-medium text-red-600 hover:bg-red-50"
                          >
                            Delete
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
      )}

      <AddProviderModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={loadProviders}
      />
      <EditProviderModal
        provider={editTarget}
        onClose={() => setEditTarget(null)}
        onUpdated={loadProviders}
      />
      <DeleteProviderModal
        provider={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onDeleted={loadProviders}
      />
    </div>
  );
}

export default ProviderOverviewTab;
