'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
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
import {
  ChangeList,
  diffFields,
  plural,
  shown,
} from '@/components/features/admin/confirm/ChangeList';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { withoutSecret } from '@/components/features/admin/confirm/withoutSecret';

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

function formatCount(value: number, singular: string): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : `${singular}s`}`;
}

function ProviderUsageCell({ keys, models }: { keys: number; models: number }) {
  return (
    <div className="flex justify-end gap-2">
      <div className="min-w-[58px] rounded-md border border-gray-100 bg-gray-50 px-2 py-1 text-right">
        <div className="text-[13px] font-semibold tabular-nums text-gray-900">{keys}</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Keys</div>
      </div>
      <div className="min-w-[58px] rounded-md border border-gray-100 bg-gray-50 px-2 py-1 text-right">
        <div className="text-[13px] font-semibold tabular-nums text-gray-900">{models}</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Models</div>
      </div>
    </div>
  );
}

interface AddProviderModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => Promise<void>;
}

function AddProviderModal({ open, onClose, onCreated }: AddProviderModalProps) {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [displayName, setDisplayName] = useState('');
  const [provider, setProvider] = useState('');
  const [providerTouched, setProviderTouched] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiKeyLabel, setApiKeyLabel] = useState('');
  const [probeModelId, setProbeModelId] = useState('');
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeProviderDefinitionResponse | null>(null);
  const [verifiedSignature, setVerifiedSignature] = useState<string | null>(null);
  const saving = confirmation.busy;

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

  // A check against the upstream that stores nothing: no confirmation.
  const onProbe = async () => {
    if (!canProbe) return;
    const key = apiKey.trim();
    setProbing(true);
    resetProbe();
    try {
      const result = await probeProviderDefinition({
        default_base_url: baseUrl.trim(),
        api_key: key,
        probe_model_id: probeModelId.trim(),
      });
      setProbeResult(result);
      setVerifiedSignature(signature);
      toast.success('Provider probe succeeded');
    } catch (err) {
      toast.error(`Probe failed: ${withoutSecret(getErrorMessage(err), key)}`);
    } finally {
      setProbing(false);
    }
  };

  // Review what will be created; only "Add provider" in the confirmation writes it.
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    const payload = {
      provider: provider.trim(),
      display_name: displayName.trim(),
      adapter_kind: 'openai_compat' as const,
      default_base_url: baseUrl.trim(),
      api_key: apiKey.trim(),
      api_key_label: apiKeyLabel.trim() || null,
      probe_model_id: probeModelId.trim(),
    };
    confirmation.request({
      title: `Add provider “${payload.display_name}”?`,
      confirmLabel: 'Add provider',
      busyLabel: 'Adding…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={[
              { label: 'Display name', after: payload.display_name },
              { label: 'Slug', after: payload.provider },
              { label: 'Adapter', after: 'OpenAI-compatible' },
              { label: 'Base URL', after: payload.default_base_url },
              { label: 'Probe model ID', after: payload.probe_model_id },
              { label: 'API key', after: 'Set (hidden)' },
              { label: 'Key label', after: shown(payload.api_key_label, '(none)') },
            ]}
          />
          <p>The provider and its first key are stored and become available to keys and routing.</p>
        </>
      ),
      run: async () => {
        await createProviderDefinition(payload).catch((err: unknown) => {
          throw new Error(withoutSecret(getErrorMessage(err), payload.api_key));
        });
        toast.success('Provider added');
        await onCreated();
        onClose();
      },
    });
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
                  placeholder="model id the upstream expects"
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
                placeholder="https://api.example.com/v1"
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
              Review provider
            </button>
          </div>
        </form>
      </div>
      {/* Rendered after the form, so the confirmation stacks over this modal. */}
      {confirmation.dialog}
    </div>
  );
}

interface EditProviderModalProps {
  provider: ProviderDefinitionItem | null;
  onClose: () => void;
  onUpdated: () => Promise<void>;
}

function EditProviderModal({ provider, onClose, onUpdated }: EditProviderModalProps) {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [displayName, setDisplayName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [probeModelId, setProbeModelId] = useState('');
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeProviderDefinitionResponse | null>(null);
  const [verifiedSignature, setVerifiedSignature] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const saving = confirmation.busy;

  useEffect(() => {
    setDisplayName(provider?.display_name ?? '');
    setBaseUrl(provider?.default_base_url ?? '');
    setApiKey('');
    setProbeModelId('');
    setProbeResult(null);
    setVerifiedSignature(null);
    setSaveError(null);
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

  if (!provider) return null;

  // The saved fields this edit changes; the key and probe model only verify a
  // new Base URL and are not stored, so they are not changes of their own.
  const changes = diffFields(
    { display_name: provider.display_name, default_base_url: provider.default_base_url },
    { display_name: displayName.trim(), default_base_url: baseUrl.trim() },
    [
      { key: 'display_name', label: 'Display name' },
      { key: 'default_base_url', label: 'Base URL' },
    ],
  );

  // A check against the upstream that stores nothing: no confirmation.
  const onProbe = async () => {
    if (!canProbe) return;
    const key = apiKey.trim();
    setProbing(true);
    resetProbe();
    try {
      const result = await probeProviderDefinition({
        default_base_url: baseUrl.trim(),
        api_key: key,
        probe_model_id: probeModelId.trim(),
      });
      setProbeResult(result);
      setVerifiedSignature(signature);
      setSaveError(null);
      toast.success('Provider probe succeeded');
    } catch (err) {
      toast.error(`Probe failed: ${withoutSecret(getErrorMessage(err), key)}`);
    } finally {
      setProbing(false);
    }
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (changes.length === 0 || probing || saving) return;
    if (!displayName.trim()) {
      setSaveError('Display name is required.');
      return;
    }
    if (!baseUrl.trim()) {
      setSaveError('Base URL is required.');
      return;
    }
    if (baseUrlNeedsProbe && !probeCurrent) {
      setSaveError('Probe the new Base URL with an API key and probe model before saving.');
      return;
    }
    setSaveError(null);
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
    const reviewed = changes;
    confirmation.request({
      title: `Save ${plural(reviewed.length, 'change')} to ${provider.display_name}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={reviewed} />
          {baseUrlNeedsProbe ? (
            <p>
              The new Base URL was probed with model {payload.probe_model_id}. The API key used for
              that check is not stored.
            </p>
          ) : null}
        </>
      ),
      run: async () => {
        await updateProviderDefinition(provider.provider, payload).catch((err: unknown) => {
          throw new Error(withoutSecret(getErrorMessage(err), payload.api_key ?? ''));
        });
        toast.success('Provider updated');
        await onUpdated();
        onClose();
      },
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
      <div className="w-full max-w-2xl rounded-lg bg-white shadow-xl">
        <form onSubmit={onSubmit} aria-label={`Edit provider ${provider.display_name}`}>
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
                  disabled={saving}
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
                disabled={saving}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>

            {baseUrlNeedsProbe && (
              <div className="space-y-2">
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
                      disabled={saving}
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
                      disabled={saving}
                      className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                      required
                    />
                  </div>
                </div>
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={onProbe}
                    disabled={probing || saving || !canProbe}
                    className="rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {probing ? 'Probing…' : probeCurrent ? 'Probe again' : 'Probe'}
                  </button>
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
          <div className="px-5 pb-4">
            <EditBar
              target={provider.display_name}
              changeCount={changes.length}
              onCancel={() => {
                if (!saving && !probing) onClose();
              }}
              busy={saving}
              error={saveError}
              submit
            />
          </div>
        </form>
      </div>
      {/* Rendered after the form, so the confirmation stacks over this modal. */}
      {confirmation.dialog}
    </div>
  );
}

interface ProviderOverviewTabProps {
  onManageKeys: (provider: string) => void;
}

export function ProviderOverviewTab({ onManageKeys }: ProviderOverviewTabProps) {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [providers, setProviders] = useState<ProviderDefinitionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<ProviderDefinitionItem | null>(null);

  const providerSummary = useMemo(
    () =>
      providers.reduce(
        (summary, provider) => ({
          keys: summary.keys + provider.keys_count,
          models: summary.models + provider.models_count,
        }),
        { keys: 0, models: 0 },
      ),
    [providers],
  );

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
  }, [toast]);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  const requestDelete = (provider: ProviderDefinitionItem) => {
    confirmation.request({
      title: `Delete provider “${provider.display_name}”?`,
      confirmLabel: 'Delete provider',
      busyLabel: 'Deleting…',
      tone: 'danger',
      requireText: provider.provider,
      body: (
        <p>
          This removes the custom provider definition {provider.provider} and its stored provider
          keys ({formatCount(provider.keys_count, 'key')}). This cannot be undone.
        </p>
      ),
      run: async () => {
        await deleteProviderDefinition(provider.provider);
        toast.success('Provider deleted');
        await loadProviders();
      },
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h3 className="text-[14px] font-semibold text-gray-900">Provider registry</h3>
          <p className="mt-1 text-[12px] text-gray-500">
            Upstream providers available to keys and routing.
          </p>
          {providers.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              <span className="rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[12px] font-medium tabular-nums text-gray-600">
                {formatCount(providers.length, 'provider')}
              </span>
              <span className="rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[12px] font-medium tabular-nums text-gray-600">
                {formatCount(providerSummary.keys, 'key')}
              </span>
              <span className="rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[12px] font-medium tabular-nums text-gray-600">
                {formatCount(providerSummary.models, 'model')}
              </span>
            </div>
          )}
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
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[840px] text-[13px]">
              <thead className="border-b border-gray-100 bg-gray-50/80 text-left text-[11px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-4 py-2.5">Provider</th>
                  <th className="px-4 py-2.5">Endpoint</th>
                  <th className="px-4 py-2.5 text-right">Resources</th>
                  <th className="px-4 py-2.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {providers.map((provider) => {
                  const custom = provider.source === 'custom';
                  const hasLinkedModels = provider.models_count > 0;
                  const deleteReasonId = `provider-delete-reason-${provider.provider}`;
                  return (
                    <tr key={provider.provider} className="transition-colors hover:bg-gray-50/70">
                      <td className="px-4 py-3.5">
                        <div className="font-medium text-gray-900">{provider.display_name}</div>
                        <div className="mt-1 font-mono text-[12px] text-gray-400">
                          {provider.provider}
                        </div>
                      </td>
                      <td className="px-4 py-3.5">
                        <div
                          className="max-w-[360px] truncate font-mono text-[12px] text-gray-700"
                          title={provider.default_base_url || undefined}
                        >
                          {formatBaseUrl(provider.default_base_url)}
                        </div>
                        <div className="mt-1 text-[11px] text-gray-400">
                          {provider.adapter_kind === 'openai_compat'
                            ? 'OpenAI-compatible'
                            : provider.adapter_kind}
                        </div>
                      </td>
                      <td className="px-4 py-3.5">
                        <ProviderUsageCell
                          keys={provider.keys_count}
                          models={provider.models_count}
                        />
                      </td>
                      <td className="px-4 py-3.5 text-right">
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => onManageKeys(provider.provider)}
                            className="rounded-md px-2.5 py-1.5 text-[12px] font-medium text-gray-700 hover:bg-gray-100"
                          >
                            Manage keys
                          </button>
                          {custom && (
                            <>
                              <button
                                type="button"
                                onClick={() => setEditTarget(provider)}
                                aria-label={`Edit ${provider.display_name}`}
                                className="rounded-md px-2.5 py-1.5 text-[12px] font-medium text-gray-700 hover:bg-gray-100"
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                disabled={hasLinkedModels}
                                aria-describedby={hasLinkedModels ? deleteReasonId : undefined}
                                onClick={() => requestDelete(provider)}
                                aria-label={`Delete ${provider.display_name}`}
                                className="rounded-md px-2.5 py-1.5 text-[12px] font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:text-gray-400 disabled:hover:bg-transparent"
                              >
                                Delete
                              </button>
                            </>
                          )}
                        </div>
                        {custom && hasLinkedModels && (
                          <p id={deleteReasonId} className="mt-1 text-[11px] text-gray-500">
                            Remove linked model routes before deleting.
                          </p>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
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
      {confirmation.dialog}
    </div>
  );
}
