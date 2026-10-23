'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  SignupAllowedDomain,
  addSignupAllowedDomain,
  listSignupAllowedDomains,
  listRuntimeSettings,
  removeSignupAllowedDomain,
  RuntimeSettingItem,
  updateRuntimeSetting,
  applyRoleQuota,
  previewRoleQuotaApply,
  Role,
  RoleQuotaPreview,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
  AdminSectionNav,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CONTROL,
  INDEXED_LAYOUT,
  PANEL,
  ROW_ACTION_DANGER,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';

import { validateNumericSettingInput } from './numericSettingValidation';
import { validateSignupDomainInput } from './signupDomainValidation';
import { AlertMutesSection } from './AlertMutesSection';
import { AlertSnoozeSection } from './AlertSnoozeSection';
import { UsageInsightsSettingsSection } from './UsageInsightsSettingsSection';
import { ModelVisibilitySection } from './ModelVisibilitySection';
import { ModelConcurrencySection } from './ModelConcurrencySection';

/** The page's sections, in order, for its section index. */
const SECTIONS = [
  { id: 'settings-feature-flags', label: 'Feature Flags' },
  { id: 'settings-numeric', label: 'Numeric Settings' },
  { id: 'settings-alert-notifications', label: 'Alert Notifications' },
  { id: 'settings-alert-types', label: 'Alert Types' },
  { id: 'settings-usage-insights', label: 'Usage Insights' },
  { id: 'settings-model-visibility', label: 'Model Visibility' },
  { id: 'settings-model-concurrency', label: 'Model Concurrency' },
  { id: 'settings-signup-policy', label: 'Signup Policy' },
] as const;

function relTime(iso: string | null): string {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

export function SettingsTab() {
  const router = useRouter();
  const [domains, setDomains] = useState<SignupAllowedDomain[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<SignupAllowedDomain | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [featureFlags, setFeatureFlags] = useState<RuntimeSettingItem[]>([]);
  const [numericSettings, setNumericSettings] = useState<RuntimeSettingItem[]>([]);
  const [numericDrafts, setNumericDrafts] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [flagsLoading, setFlagsLoading] = useState(true);
  const [flagsError, setFlagsError] = useState<string | null>(null);
  const [togglingKey, setTogglingKey] = useState<string | null>(null);

  const QUOTA_KEY_PREFIX = 'user_daily_quota_';
  const [quotaConfirm, setQuotaConfirm] = useState<RoleQuotaPreview | null>(null);
  const [quotaLoadingRole, setQuotaLoadingRole] = useState<Role | null>(null);
  const [quotaApplyingRole, setQuotaApplyingRole] = useState<Role | null>(null);

  const loadDomains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listSignupAllowedDomains();
      setDomains(resp.domains);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadFlags = useCallback(async () => {
    setFlagsLoading(true);
    setFlagsError(null);
    try {
      const resp = await listRuntimeSettings();
      setFeatureFlags(resp.settings.filter((s) => s.value_type === 'bool'));
      const numerics = resp.settings.filter(
        (s) => s.value_type === 'int' || s.value_type === 'float',
      );
      setNumericSettings(numerics);
      setNumericDrafts(Object.fromEntries(numerics.map((s) => [s.key, String(s.value ?? '')])));
    } catch (e) {
      setFlagsError(getErrorMessage(e));
    } finally {
      setFlagsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDomains();
    loadFlags();
  }, [loadDomains, loadFlags]);

  useEffect(
    () => () => {
      if (toastTimeoutRef.current !== null) {
        clearTimeout(toastTimeoutRef.current);
        toastTimeoutRef.current = null;
      }
    },
    [],
  );

  const flashToast = (msg: string) => {
    if (toastTimeoutRef.current !== null) {
      clearTimeout(toastTimeoutRef.current);
    }
    setToast(msg);
    toastTimeoutRef.current = setTimeout(() => {
      setToast(null);
      toastTimeoutRef.current = null;
    }, 3000);
  };

  /**
   * Re-render the server components after a runtime setting changed. The site
   * configuration they read follows some of these settings: sign-up and email
   * verification decide whether the email (SMTP) settings are required, and
   * user authentication whether the sign-in secrets are — so whether the
   * "required settings are missing" banner shows.
   */
  const refreshSiteConfig = () => router.refresh();

  const onToggleFlag = async (flag: RuntimeSettingItem) => {
    setTogglingKey(flag.key);
    try {
      const updatedFlag = await updateRuntimeSetting(flag.key, !flag.value);
      flashToast(`${updatedFlag.key} ${updatedFlag.value ? 'enabled' : 'disabled'}`);
      setFeatureFlags((prev) => prev.map((f) => (f.key === updatedFlag.key ? updatedFlag : f)));
      refreshSiteConfig();
    } catch (e) {
      flashToast(`Failed to update ${flag.key}: ${getErrorMessage(e)}`);
    } finally {
      setTogglingKey(null);
    }
  };

  const onSaveNumeric = async (setting: RuntimeSettingItem) => {
    const draft = numericDrafts[setting.key] ?? '';
    const validated = validateNumericSettingInput(draft, {
      min: setting.min,
      max: setting.max,
      integer: setting.value_type === 'int',
    });
    if (!validated.ok) {
      flashToast(`${setting.key}: ${validated.error}`);
      return;
    }
    setSavingKey(setting.key);
    try {
      const updated = await updateRuntimeSetting(setting.key, validated.value);
      flashToast(`${updated.key} set to ${updated.value}`);
      setNumericSettings((prev) => prev.map((s) => (s.key === updated.key ? updated : s)));
      setNumericDrafts((prev) => ({ ...prev, [updated.key]: String(updated.value ?? '') }));
      refreshSiteConfig();
    } catch (e) {
      flashToast(`Failed to update ${setting.key}: ${getErrorMessage(e)}`);
    } finally {
      setSavingKey(null);
    }
  };

  const onAdd = async () => {
    setInputError(null);
    const validated = validateSignupDomainInput(input);
    if (!validated.ok) {
      setInputError(validated.error);
      return;
    }
    setAdding(true);
    try {
      await addSignupAllowedDomain(input.trim());
      setInput('');
      flashToast(
        validated.isWildcard
          ? `Added wildcard *.${validated.domain}.`
          : `Added ${validated.domain}.`,
      );
      await loadDomains();
    } catch (e) {
      setInputError(getErrorMessage(e));
    } finally {
      setAdding(false);
    }
  };

  const onRemove = async (row: SignupAllowedDomain) => {
    const key = `${row.domain}|${row.is_wildcard}`;
    setRemoving(key);
    try {
      await removeSignupAllowedDomain(row.domain, row.is_wildcard);
      flashToast(row.is_wildcard ? `Removed wildcard *.${row.domain}.` : `Removed ${row.domain}.`);
      await loadDomains();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setRemoving(null);
      setConfirm(null);
    }
  };

  const onClickApply = async (role: Role) => {
    setQuotaLoadingRole(role);
    try {
      const preview = await previewRoleQuotaApply(role);
      setQuotaConfirm(preview);
    } catch (e) {
      flashToast(`Preview failed: ${getErrorMessage(e)}`);
    } finally {
      setQuotaLoadingRole(null);
    }
  };

  const onConfirmApply = async () => {
    if (!quotaConfirm) return;
    const role = quotaConfirm.role;
    setQuotaApplyingRole(role);
    try {
      const res = await applyRoleQuota(role);
      flashToast(`Updated ${res.keys_updated} keys for role ${role} to $${res.quota}`);
      setQuotaConfirm(null);
    } catch (e) {
      flashToast(`Apply failed: ${getErrorMessage(e)}`);
    } finally {
      setQuotaApplyingRole(null);
    }
  };

  return (
    // A section index beside full-width sections, one after another.
    <div className={`mt-6 ${INDEXED_LAYOUT}`}>
      <AdminSectionNav label="Settings sections" items={SECTIONS} />

      <div className="min-w-0">
        {/* Feedback for every section on this page: above the sections, and
          pinned to the top of the window while they scroll, so it is in view
          whichever section the change was made in. */}
        {toast && (
          <div
            role="status"
            className="sticky top-4 z-20 mb-4 rounded-lg bg-gray-900 px-3 py-2 text-[12px] text-white shadow-lg"
          >
            {toast}
          </div>
        )}

        <div className="space-y-6 [&>*+*]:border-t [&>*+*]:border-gray-200 [&>*+*]:pt-6">
          {/* Feature Flags */}
          <AdminSection
            id="settings-feature-flags"
            title="Feature Flags"
            description="Toggle runtime features without restarting the server. Changes take effect immediately."
          >
            {flagsError && (
              <div className="mb-3">
                <AdminNotice onDismiss={() => setFlagsError(null)}>{flagsError}</AdminNotice>
              </div>
            )}

            {flagsLoading ? (
              <AdminLoading size="sm" />
            ) : featureFlags.length === 0 ? (
              <AdminEmptyState title="No feature flags available." />
            ) : (
              <ul className={`${PANEL} divide-y divide-gray-100`}>
                {featureFlags.map((flag) => {
                  const isToggling = togglingKey === flag.key;
                  const isOn = !!flag.value;
                  const isDefault = flag.value === flag.default_value;
                  return (
                    <li
                      key={flag.key}
                      className="flex items-center justify-between gap-4 px-4 py-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-medium text-gray-900">
                            {flag.key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                          </span>
                          {!isDefault && (
                            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                              Modified
                            </span>
                          )}
                        </div>
                        <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">
                          {flag.description}
                        </p>
                      </div>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={isOn}
                        aria-label={`Toggle ${flag.key.replace(/_/g, ' ')}`}
                        disabled={isToggling}
                        onClick={() => onToggleFlag(flag)}
                        className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-40 ${
                          isOn ? 'bg-gray-900' : 'bg-gray-200'
                        }`}
                      >
                        <span
                          className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                            isOn ? 'translate-x-5' : 'translate-x-0'
                          }`}
                        />
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </AdminSection>

          {/* Numeric Settings */}
          <AdminSection
            id="settings-numeric"
            title="Numeric Settings"
            description="Adjust numeric runtime knobs (e.g. per-user concurrency caps). Changes take effect immediately."
          >
            {flagsLoading ? (
              <AdminLoading size="sm" />
            ) : numericSettings.length === 0 ? (
              <AdminEmptyState title="No numeric settings available." />
            ) : (
              <ul className={`${PANEL} divide-y divide-gray-100`}>
                {numericSettings.map((setting) => {
                  const isSaving = savingKey === setting.key;
                  const draft = numericDrafts[setting.key] ?? '';
                  const validated = validateNumericSettingInput(draft, {
                    min: setting.min,
                    max: setting.max,
                    integer: setting.value_type === 'int',
                  });
                  const isDirty = validated.ok && validated.value !== setting.value;
                  const isDefault = setting.value === setting.default_value;
                  const rangeHint = (() => {
                    const lo = setting.min;
                    const hi = setting.max;
                    if (lo != null && hi != null) return `${lo}–${hi}`;
                    if (lo != null) return `≥ ${lo}`;
                    if (hi != null) return `≤ ${hi}`;
                    return null;
                  })();
                  return (
                    <li
                      key={setting.key}
                      className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-medium text-gray-900">
                            {setting.key
                              .replace(/_/g, ' ')
                              .replace(/\b\w/g, (c) => c.toUpperCase())}
                          </span>
                          {!isDefault && (
                            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                              Modified
                            </span>
                          )}
                        </div>
                        <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">
                          {setting.description}
                          {rangeHint && (
                            <span className="ml-1 text-gray-400">(range: {rangeHint})</span>
                          )}
                          <span className="ml-1 text-gray-400">
                            Default: {String(setting.default_value)}
                          </span>
                        </p>
                        {!validated.ok && draft !== '' && (
                          <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                            {validated.error}
                          </p>
                        )}
                      </div>
                      <div className="flex shrink-0 flex-wrap items-center gap-2">
                        <input
                          type="number"
                          step={setting.value_type === 'int' ? 1 : 'any'}
                          min={setting.min ?? undefined}
                          max={setting.max ?? undefined}
                          value={draft}
                          disabled={isSaving}
                          onChange={(e) =>
                            setNumericDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
                          }
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && isDirty && !isSaving) {
                              e.preventDefault();
                              onSaveNumeric(setting);
                            }
                          }}
                          aria-label={`${setting.key} value`}
                          className={`${CONTROL} w-24 text-right`}
                        />
                        <button
                          type="button"
                          onClick={() => onSaveNumeric(setting)}
                          disabled={!isDirty || isSaving}
                          className={BUTTON_PRIMARY}
                        >
                          {isSaving ? 'Saving...' : 'Save'}
                        </button>
                        {setting.key.startsWith(QUOTA_KEY_PREFIX) && (
                          <button
                            type="button"
                            onClick={() =>
                              onClickApply(setting.key.slice(QUOTA_KEY_PREFIX.length) as Role)
                            }
                            disabled={quotaLoadingRole !== null || quotaApplyingRole !== null}
                            className={BUTTON_SECONDARY}
                          >
                            {quotaLoadingRole === setting.key.slice(QUOTA_KEY_PREFIX.length)
                              ? 'Loading...'
                              : 'Apply to existing users'}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </AdminSection>

          <div id="settings-alert-notifications" className="scroll-mt-4">
            <AlertSnoozeSection onToast={flashToast} />
          </div>

          <div id="settings-alert-types" className="scroll-mt-4">
            <AlertMutesSection onToast={flashToast} />
          </div>

          <div id="settings-usage-insights" className="scroll-mt-4">
            <UsageInsightsSettingsSection onToast={flashToast} />
          </div>

          <div id="settings-model-visibility" className="scroll-mt-4">
            <ModelVisibilitySection onToast={flashToast} />
          </div>

          <div id="settings-model-concurrency" className="scroll-mt-4">
            <ModelConcurrencySection onToast={flashToast} />
          </div>

          {/* Signup Policy */}
          <AdminSection
            id="settings-signup-policy"
            title="Signup Policy"
            description="Signups from listed domains auto-approve. Other domains require admin approval. Empty list = all signups auto-approve."
          >
            {/* Add form */}
            <div className="mb-4 flex max-w-xl flex-col gap-1.5">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value);
                    setInputError(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !adding) {
                      e.preventDefault();
                      onAdd();
                    }
                  }}
                  placeholder="example.com or *.example.com"
                  className={`${CONTROL} min-w-0 flex-1`}
                  disabled={adding}
                  aria-label="Domain to add"
                />
                <button type="button" onClick={onAdd} disabled={adding} className={BUTTON_PRIMARY}>
                  {adding ? 'Adding...' : 'Add'}
                </button>
              </div>
              <p className="text-[11px] text-gray-400">
                Enter <code className="rounded bg-gray-100 px-1">example.com</code> for an exact
                match or <code className="rounded bg-gray-100 px-1">*.example.com</code> to allow
                any subdomain. <code className="rounded bg-gray-100 px-1">*.example.com</code> does
                not match the bare suffix.
              </p>
              {inputError && (
                <p className="text-[12px] text-red-600" role="alert">
                  {inputError}
                </p>
              )}
            </div>

            {/* Status */}
            {error && (
              <div className="mb-3">
                <AdminNotice onDismiss={() => setError(null)}>{error}</AdminNotice>
              </div>
            )}
            {/* List */}
            {loading ? (
              <AdminLoading size="sm" />
            ) : domains.length === 0 ? (
              <AdminEmptyState title="No allowed domains. All signups auto-approve." />
            ) : (
              <div className={`${PANEL} overflow-x-auto`}>
                <table className="w-full min-w-[30rem] text-[13px]">
                  <thead className="bg-gray-50/80">
                    <tr className="border-b border-gray-200">
                      <th className={TABLE_HEAD_CELL}>Domain</th>
                      <th className={TABLE_HEAD_CELL}>Type</th>
                      <th className={TABLE_HEAD_CELL}>Added</th>
                      <th className={`${TABLE_HEAD_CELL} text-right`}>Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {domains.map((d) => {
                      const key = `${d.domain}|${d.is_wildcard}`;
                      const isRemoving = removing === key;
                      return (
                        <tr key={key}>
                          <td className="px-4 py-2 font-mono text-[12px] text-gray-900">
                            {d.is_wildcard ? <span>*.{d.domain}</span> : d.domain}
                          </td>
                          <td className="px-4 py-2 text-gray-500">
                            {d.is_wildcard ? 'Wildcard' : 'Exact'}
                          </td>
                          <td className="whitespace-nowrap px-4 py-2 text-gray-500">
                            {relTime(d.created_at)}
                          </td>
                          <td className="px-4 py-2 text-right">
                            <button
                              type="button"
                              onClick={() => setConfirm(d)}
                              disabled={isRemoving}
                              className={ROW_ACTION_DANGER}
                            >
                              {isRemoving ? 'Removing...' : 'Remove'}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </AdminSection>
        </div>
      </div>

      {/* Quota apply confirm dialog */}
      {quotaConfirm && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
          onClick={() => quotaApplyingRole === null && setQuotaConfirm(null)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-[15px] font-semibold text-gray-900">
              Apply quota to role <code>{quotaConfirm.role}</code>
            </h3>
            <p className="mt-2 text-[13px] text-gray-600">
              This sets <code>quota_daily_cost_usd = ${quotaConfirm.quota.toString()}</code> on{' '}
              <strong>{quotaConfirm.keys_affected}</strong> active API keys belonging to{' '}
              <strong>{quotaConfirm.users_affected}</strong> users with role{' '}
              <code>{quotaConfirm.role}</code>. Custom per-key overrides will be lost.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setQuotaConfirm(null)}
                disabled={quotaApplyingRole !== null}
                className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onConfirmApply}
                disabled={quotaApplyingRole !== null}
                className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
              >
                {quotaApplyingRole !== null ? 'Applying...' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Confirm dialog */}
      {confirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
          onClick={() => setConfirm(null)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-[15px] font-semibold text-gray-900">Remove allowed domain</h3>
            <p className="mt-2 text-[13px] text-gray-600">
              Future signups from{' '}
              <code className="rounded bg-gray-100 px-1">
                {confirm.is_wildcard ? `*.${confirm.domain}` : confirm.domain}
              </code>{' '}
              will require admin approval. Existing users keep their current status.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirm(null)}
                className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => onRemove(confirm)}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
