'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { ChangeList, plural, shown } from '@/components/features/admin/confirm/ChangeList';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { withoutSecret } from '@/components/features/admin/confirm/withoutSecret';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import {
  ProviderApiKeyItem,
  ProviderKeyMinRole,
  addProviderKey,
  deleteProviderKey,
  disableProviderEnvKey,
  enableProviderEnvKey,
  listProviderKeyProviders,
  listProviderKeys,
  setProviderEnvKeyMinRole,
  setProviderKeyMinRole,
  setProviderKeyStatus,
  verifyProviderKey,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { Select } from '@/components/ui/Select';

interface Props {
  refreshKey?: number;
  initialProvider?: string;
  onProviderChange?: (provider: string) => void;
}

const MIN_ROLES: ProviderKeyMinRole[] = ['free', 'pro', 'internal', 'admin'];

// Why adding a key waits: it would switch away from unsaved tier changes.
const TIERS_LOCKED = 'Save or discard the tier changes first';

// Label for the reservation column. 'free' is the absence of a reservation, so
// it reads as "shared" rather than as a tier name.
function minRoleLabel(minRole: ProviderKeyMinRole): string {
  return minRole === 'free' ? 'shared' : `${minRole}+`;
}

// The tier this row declares, which is what editing it changes.
function declaredTier(key: ProviderApiKeyItem): ProviderKeyMinRole {
  return key.declared_min_role ?? key.min_role;
}

// Env and DB ids live in different namespaces, so a draft is keyed by both.
function draftId(key: ProviderApiKeyItem): string {
  return `${key.source}:${key.id}`;
}

// How a key is named in confirmations: its masked prefix, never the secret.
function keyName(key: ProviderApiKeyItem): string {
  return key.label ? `${key.key_prefix} (${key.label})` : key.key_prefix;
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

export function ProviderKeysTab({ refreshKey = 0, initialProvider = '', onProviderChange }: Props) {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [providers, setProviders] = useState<string[]>([]);
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState<string>(initialProvider);
  const [keys, setKeys] = useState<ProviderApiKeyItem[]>([]);
  const [loading, setLoading] = useState(false);

  const [addOpen, setAddOpen] = useState(false);
  const [formProvider, setFormProvider] = useState<string>(initialProvider);
  const [formApiKey, setFormApiKey] = useState('');
  const [formLabel, setFormLabel] = useState('');
  const [formMinRole, setFormMinRole] = useState<ProviderKeyMinRole>('free');
  const [verifyingKey, setVerifyingKey] = useState(false);
  const [verifiedKeySignature, setVerifiedKeySignature] = useState<string | null>(null);

  // draftId → the chosen tier; only real changes are kept.
  const [tierDrafts, setTierDrafts] = useState<Record<string, ProviderKeyMinRole>>({});
  const [tierSaveError, setTierSaveError] = useState<string | null>(null);

  const loadProviders = useCallback(async () => {
    try {
      const resp = await listProviderKeyProviders();
      const names = resp.providers;
      setProviders(names);
      setProvidersLoaded(true);
      if (names.length > 0) {
        setSelectedProvider((current) => current || names[0]);
        setFormProvider((current) => current || names[0]);
      }
    } catch (err) {
      toast.error(`Failed to load providers: ${getErrorMessage(err)}`);
    }
  }, [toast]);

  const loadKeys = useCallback(
    async (provider: string) => {
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
    },
    [toast],
  );

  useEffect(() => {
    void loadProviders();
  }, [loadProviders, refreshKey]);

  useEffect(() => {
    if (providers.includes(selectedProvider)) void loadKeys(selectedProvider);
  }, [loadKeys, providers, selectedProvider, refreshKey]);

  useEffect(() => {
    onProviderChange?.(selectedProvider);
  }, [onProviderChange, selectedProvider]);

  // Tier drafts and their error belong to the provider shown, so a switch drops
  // what is left of them; it is only possible while no tier change is unsaved.
  const showProvider = (provider: string) => {
    setSelectedProvider(provider);
    setTierDrafts({});
    setTierSaveError(null);
  };

  const resetAddForm = () => {
    setFormApiKey('');
    setFormLabel('');
    setFormMinRole('free');
    setVerifiedKeySignature(null);
  };

  const formKeySignature =
    formProvider && formApiKey.trim() ? `${formProvider}\u0000${formApiKey.trim()}` : null;
  const keyVerified = verifiedKeySignature != null && verifiedKeySignature === formKeySignature;

  // Review what will be created; only "Add key" in the confirmation writes it.
  const onReviewAdd = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (tiersChanged || !providers.includes(formProvider) || !formApiKey.trim()) return;
    const provider = formProvider;
    const apiKey = formApiKey.trim();
    const label = formLabel.trim();
    const minRole = formMinRole;
    confirmation.request({
      title: `Add a key to ${provider}?`,
      confirmLabel: 'Add key',
      busyLabel: 'Adding…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={[
              { label: 'Provider', after: provider },
              { label: 'Label', after: shown(label, '(none)') },
              { label: 'Reserved for', after: minRoleLabel(minRole) },
              { label: 'API key', after: 'Set (hidden)' },
              { label: 'Verified', after: keyVerified ? 'Yes' : 'Not verified' },
            ]}
          />
          <p>The key joins the {provider} pool and can serve traffic as soon as it is added.</p>
        </>
      ),
      run: async () => {
        const resp = await addProviderKey(provider, apiKey, label || undefined, minRole).catch(
          (err: unknown) => {
            throw new Error(withoutSecret(getErrorMessage(err), apiKey));
          },
        );
        if (resp.pools_updated === 0) {
          toast.error(
            'Key saved but not attached to any live pool — it will NOT be used ' +
              'for inference. This provider has no multi-key-capable adapter loaded.',
          );
        } else {
          toast.success(`Key added (${resp.pools_updated} pool(s) updated)`);
        }
        resetAddForm();
        setAddOpen(false);
        if (provider === selectedProvider) {
          await loadKeys(selectedProvider);
        } else {
          showProvider(provider);
        }
      },
    });
  };

  const cancelAdd = () => {
    if (confirmation.busy) return;
    resetAddForm();
    setAddOpen(false);
  };

  // A check against the upstream that stores nothing: no confirmation.
  const onVerifyKey = async () => {
    if (!providers.includes(formProvider) || !formApiKey.trim() || !formKeySignature) return;
    const apiKey = formApiKey.trim();
    setVerifyingKey(true);
    try {
      await verifyProviderKey(formProvider, apiKey);
      setVerifiedKeySignature(formKeySignature);
      toast.success('Provider key verified');
    } catch (err) {
      setVerifiedKeySignature(null);
      toast.error(`Verify failed: ${withoutSecret(getErrorMessage(err), apiKey)}`);
    } finally {
      setVerifyingKey(false);
    }
  };

  const requestDelete = (key: ProviderApiKeyItem) => {
    const id = key.id;
    if (!id) return;
    const provider = selectedProvider;
    confirmation.request({
      title: `Delete key ${key.key_prefix}?`,
      confirmLabel: 'Delete',
      busyLabel: 'Deleting…',
      tone: 'danger',
      body: (
        <p>
          The {key.provider} key {keyName(key)} is removed from its pools and deleted. This cannot
          be undone.
        </p>
      ),
      run: async () => {
        await deleteProviderKey(id);
        toast.success('Key deleted');
        await loadKeys(provider);
      },
    });
  };

  const requestEnvStatus = (key: ProviderApiKeyItem, enable: boolean) => {
    const id = key.id;
    if (!id) return;
    const provider = selectedProvider;
    confirmation.request({
      title: `${enable ? 'Enable' : 'Disable'} env key ${key.key_prefix}?`,
      confirmLabel: enable ? 'Enable' : 'Disable',
      busyLabel: enable ? 'Enabling…' : 'Disabling…',
      body: enable ? (
        <p>
          The {key.provider} env key {keyName(key)} is re-added to its pools and serves traffic
          again.
        </p>
      ) : (
        <p>
          The {key.provider} env key {keyName(key)} stops being used immediately. You can enable it
          again later.
        </p>
      ),
      run: async () => {
        if (enable) {
          const resp = await enableProviderEnvKey(key.provider, id);
          if (resp.pools_updated === 0) {
            toast.error('Env key re-enabled but could not be re-added to any pool.');
          } else {
            toast.success('Env key enabled');
          }
        } else {
          await disableProviderEnvKey(key.provider, id);
          toast.success('Env key disabled');
        }
        await loadKeys(provider);
      },
    });
  };

  const requestDbStatus = (key: ProviderApiKeyItem, enable: boolean) => {
    const id = key.id;
    if (!id) return;
    const provider = selectedProvider;
    confirmation.request({
      title: `${enable ? 'Enable' : 'Disable'} key ${key.key_prefix}?`,
      confirmLabel: enable ? 'Enable' : 'Disable',
      busyLabel: enable ? 'Enabling…' : 'Disabling…',
      body: enable ? (
        <p>
          The {key.provider} key {keyName(key)} rejoins its pools and serves traffic again.
        </p>
      ) : (
        <p>
          The {key.provider} key {keyName(key)} stops serving traffic immediately. You can enable it
          again later.
        </p>
      ),
      run: async () => {
        const resp = await setProviderKeyStatus(id, enable);
        if (enable && resp.pools_updated === 0) {
          toast.error('Key enabled but not attached to any live pool — it will not be used.');
        } else {
          toast.success(enable ? 'Key enabled' : 'Key disabled');
        }
        await loadKeys(provider);
      },
    });
  };

  const sortedKeys = useMemo(
    () => [...keys].sort((a, b) => (a.source === b.source ? 0 : a.source === 'db' ? -1 : 1)),
    [keys],
  );

  const tiersTarget = `${selectedProvider} key tiers`;
  const changedTiers = sortedKeys.filter((k) => k.id && draftId(k) in tierDrafts);
  // Unsaved tier changes hold the provider selector and adding a key, which
  // would switch the page to another provider's keys.
  const tiersChanged = changedTiers.length > 0;

  const changeTierDraft = (key: ProviderApiKeyItem, value: ProviderKeyMinRole) => {
    setTierDrafts((prev) => {
      const next = { ...prev };
      if (value === declaredTier(key)) delete next[draftId(key)];
      else next[draftId(key)] = value;
      return next;
    });
  };

  const cancelTiers = () => {
    if (confirmation.busy) return;
    setTierDrafts({});
    setTierSaveError(null);
  };

  const saveTiers = () => {
    if (changedTiers.length === 0) return;
    const provider = selectedProvider;
    const pending = changedTiers.map((key) => ({ key, minRole: tierDrafts[draftId(key)] }));
    confirmation.request({
      title: `Save ${plural(pending.length, 'tier change')} for ${provider} keys?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ key, minRole }) => ({
              label: keyName(key),
              before: minRoleLabel(declaredTier(key)),
              after: minRoleLabel(minRole),
            }))}
          />
          <p>
            A reserved key is hidden from lower tiers as soon as this is saved; &quot;shared&quot;
            lets every tier spend it.
          </p>
        </>
      ),
      run: async () => {
        const unpooled: string[] = [];
        const failure = await writeEach(
          pending,
          ({ key }) => keyName(key),
          async ({ key, minRole }) => {
            const id = key.id as string;
            // Env keys carry no row id, so they are addressed by provider + env id.
            const resp =
              key.source === 'env'
                ? await setProviderEnvKeyMinRole(key.provider, id, minRole)
                : await setProviderKeyMinRole(id, minRole);
            if (resp.pools_updated === 0) unpooled.push(keyName(key));
            setTierDrafts((prev) => {
              const next = { ...prev };
              delete next[draftId(key)];
              return next;
            });
          },
        );
        if (!failure) {
          const only = pending[0].minRole;
          toast.success(
            pending.length > 1
              ? `Updated reserved tiers for ${pending.length} keys`
              : only === 'free'
                ? 'Key shared with every tier'
                : `Key reserved for ${only} and above`,
          );
        }
        if (unpooled.length > 0) {
          toast.error(
            `Saved, but no live pool holds ${
              pending.length === 1 ? 'this key' : unpooled.join(', ')
            } right now — the new tier applies once it is back in rotation.`,
          );
        }
        await loadKeys(provider);
        setTierSaveError(failure);
      },
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <label
          className="block text-[12px] font-medium text-gray-500"
          htmlFor="provider-keys-select"
        >
          Selected provider
        </label>
        <Select
          id="provider-keys-select"
          value={selectedProvider}
          onValueChange={(next) => {
            showProvider(next);
            setFormProvider(next);
          }}
          options={[
            ...(selectedProvider && !providers.includes(selectedProvider)
              ? [{ value: selectedProvider, label: selectedProvider }]
              : []),
            ...providers.map((p) => ({ value: p, label: p })),
          ]}
          placeholder="(no providers loaded)"
          searchPlaceholder="Search providers…"
          // Tier drafts belong to this provider's keys; save or discard them first.
          disabled={tiersChanged}
          size="lg"
          className="mt-1 w-full max-w-xs"
        />
      </div>

      {providersLoaded && selectedProvider && !providers.includes(selectedProvider) && (
        <p className="text-[13px] text-gray-500">
          Key management is unavailable for {selectedProvider}. Choose another provider above.
        </p>
      )}

      <div hidden={!providers.includes(selectedProvider)}>
        <h3 className="text-[14px] font-semibold text-gray-900">Configured keys</h3>
        {loading ? (
          <div className="flex justify-center py-12">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          </div>
        ) : sortedKeys.length === 0 ? (
          <p className="mt-3 text-[13px] text-gray-400">No keys configured for this provider.</p>
        ) : (
          <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200">
            <table className="min-w-[680px] w-full text-[13px]">
              <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2">Prefix</th>
                  <th className="px-3 py-2">Label</th>
                  <th className="px-3 py-2">Source</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Reserved for</th>
                  <th className="px-3 py-2">Created</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {sortedKeys.map((k) => {
                  const disabled = k.status === 'disabled';
                  const drafted = k.id != null && draftId(k) in tierDrafts;
                  return (
                    <tr
                      key={`${k.source}-${k.id ?? k.key_prefix}`}
                      className={disabled ? 'opacity-60' : ''}
                    >
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
                      <td className="px-3 py-2">
                        <span
                          className={
                            k.status === 'active'
                              ? 'rounded bg-green-50 px-1.5 py-0.5 text-[11px] font-medium text-green-700'
                              : 'rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-500'
                          }
                          title={
                            k.status === 'absent'
                              ? 'A reservation is stored for this credential, but nothing is configured with it right now. It applies again if the key returns.'
                              : undefined
                          }
                        >
                          {/* Rendered verbatim rather than as a disabled/active
                              binary: a stored reservation whose credential is not
                              configured anywhere is neither. */}
                          {k.status}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        {k.id ? (
                          <div className="flex items-center gap-1.5">
                            <Select<ProviderKeyMinRole>
                              aria-label={`Reserved tier for key ${k.key_prefix}`}
                              value={tierDrafts[draftId(k)] ?? declaredTier(k)}
                              // A draft only; Save changes reviews it and Confirm writes.
                              onValueChange={(next) => changeTierDraft(k, next)}
                              disabled={confirmation.busy}
                              options={MIN_ROLES.map((r) => ({
                                value: r,
                                label: minRoleLabel(r),
                              }))}
                              size="sm"
                              className={`w-28 ${drafted ? '!border-amber-400' : ''}`}
                            />
                            {declaredTier(k) !== k.min_role && (
                              // Another record declares something stricter for this
                              // same credential, and the pool enforces that one — say
                              // so, or editing this row looks like it did nothing.
                              <span
                                className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700"
                                title="Another record declares a stricter tier for this same credential, so that is what is enforced."
                              >
                                {minRoleLabel(k.min_role)} enforced
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="text-[12px] text-gray-400">
                            {minRoleLabel(k.min_role)}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-gray-500">{formatRelative(k.created_at)}</td>
                      <td className="px-3 py-2 text-right">
                        <div className="flex justify-end gap-1">
                          {k.reservation_only ? (
                            // Listed only to expose its reservation: an active DB row
                            // holds this same credential and owns enable/disable/
                            // delete. Disabling the env side would tombstone a hash
                            // while the key kept serving from that row, which the
                            // endpoint rejects — so offer no action here.
                            <span
                              className="px-2 py-1 text-[12px] text-gray-400"
                              title="A DB key holds this same credential — manage it on that row. This entry only carries the reservation."
                            >
                              reservation only
                            </span>
                          ) : k.source === 'env' ? (
                            disabled ? (
                              <button
                                type="button"
                                onClick={() => requestEnvStatus(k, true)}
                                disabled={!k.id}
                                aria-label={`Enable key ${k.key_prefix}`}
                                className="rounded-md px-2 py-1 text-[12px] font-medium text-green-700 hover:bg-green-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                              >
                                Enable
                              </button>
                            ) : (
                              <button
                                type="button"
                                onClick={() => requestEnvStatus(k, false)}
                                disabled={!k.id}
                                aria-label={`Disable key ${k.key_prefix}`}
                                className="rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                              >
                                Disable
                              </button>
                            )
                          ) : (
                            <>
                              <button
                                type="button"
                                onClick={() => requestDbStatus(k, disabled)}
                                disabled={!k.id}
                                aria-label={`${disabled ? 'Enable' : 'Disable'} key ${k.key_prefix}`}
                                className={
                                  disabled
                                    ? 'rounded-md px-2 py-1 text-[12px] font-medium text-green-700 hover:bg-green-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent'
                                    : 'rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent'
                                }
                              >
                                {disabled ? 'Enable' : 'Disable'}
                              </button>
                              <button
                                type="button"
                                onClick={() => requestDelete(k)}
                                disabled={!k.id}
                                aria-label={`Delete key ${k.key_prefix}`}
                                className="rounded-md px-2 py-1 text-[12px] font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                              >
                                Delete
                              </button>
                            </>
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
        {/* The confirmation also serves the key actions: they must not bring up this bar. */}
        <EditBar
          target={tiersTarget}
          changeCount={changedTiers.length}
          onCancel={cancelTiers}
          onSave={saveTiers}
          busy={confirmation.busy && tiersChanged}
          error={tierSaveError}
          inPlace
        />
      </div>

      <div
        hidden={!providers.includes(selectedProvider)}
        className="space-y-3 rounded-lg border border-gray-200 bg-white p-4"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-[14px] font-semibold text-gray-900">Add a new key</h3>
          {!addOpen ? (
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              // Adding a key switches to its provider, away from unsaved tier drafts.
              disabled={tiersChanged}
              title={tiersChanged ? TIERS_LOCKED : undefined}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Add key
            </button>
          ) : null}
        </div>
        {addOpen ? (
          <form aria-label="Add a new key" onSubmit={onReviewAdd} className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-keys-form-provider"
                >
                  Provider
                </label>
                <Select
                  id="provider-keys-form-provider"
                  value={formProvider}
                  onValueChange={setFormProvider}
                  options={providers.map((p) => ({ value: p, label: p }))}
                  searchPlaceholder="Search providers…"
                  disabled={confirmation.busy}
                  aria-required
                  size="lg"
                  className="mt-1 w-full"
                />
              </div>
              <div>
                <label
                  className="block text-[12px] font-medium text-gray-500"
                  htmlFor="provider-keys-form-label"
                >
                  Label (optional)
                </label>
                <input
                  id="provider-keys-form-label"
                  type="text"
                  value={formLabel}
                  onChange={(e) => setFormLabel(e.target.value)}
                  disabled={confirmation.busy}
                  maxLength={255}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                />
              </div>
            </div>
            <div>
              <label
                className="block text-[12px] font-medium text-gray-500"
                htmlFor="provider-keys-form-min-role"
              >
                Reserved for
              </label>
              <Select<ProviderKeyMinRole>
                id="provider-keys-form-min-role"
                value={formMinRole}
                onValueChange={setFormMinRole}
                options={MIN_ROLES.map((r) => ({ value: r, label: minRoleLabel(r) }))}
                disabled={confirmation.busy}
                size="lg"
                className="mt-1 w-full max-w-xs"
              />
              <p className="mt-1 text-[12px] text-gray-400">
                &quot;shared&quot; lets every tier spend this key. Anything higher hides it from
                lower tiers, and entitled users spend it before the shared keys. Existing keys —
                env-sourced ones included — can be re-tiered from the table above.
              </p>
            </div>
            <div>
              <label
                className="block text-[12px] font-medium text-gray-500"
                htmlFor="provider-keys-form-key"
              >
                API key
              </label>
              <input
                id="provider-keys-form-key"
                type="password"
                value={formApiKey}
                onChange={(e) => setFormApiKey(e.target.value)}
                disabled={confirmation.busy}
                autoComplete="off"
                spellCheck={false}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-mono focus:border-gray-400 focus:outline-none"
                required
              />
            </div>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={cancelAdd}
                disabled={confirmation.busy}
                className="rounded-md px-4 py-2 text-[13px] font-medium text-gray-600 transition hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onVerifyKey}
                disabled={verifyingKey || confirmation.busy || !formProvider || !formApiKey.trim()}
                className={
                  keyVerified
                    ? 'rounded-md border border-emerald-600 bg-emerald-600 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40'
                    : 'rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40'
                }
              >
                {verifyingKey ? 'Verifying…' : keyVerified ? 'Verified' : 'Verify'}
              </button>
              <button
                type="submit"
                disabled={
                  confirmation.busy ||
                  verifyingKey ||
                  tiersChanged ||
                  !formProvider ||
                  !formApiKey.trim()
                }
                title={tiersChanged ? TIERS_LOCKED : undefined}
                className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Review key
              </button>
            </div>
          </form>
        ) : null}
      </div>
      {confirmation.dialog}
    </div>
  );
}
