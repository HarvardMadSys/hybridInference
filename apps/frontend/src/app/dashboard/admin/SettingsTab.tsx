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
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
  AdminSectionNav,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CONTROL,
  INDEXED_LAYOUT,
  PANEL,
  ROW_ACTION_DANGER,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import { getErrorMessage } from '@/lib/utils/errors';

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

function settingLabel(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function rangeHint(setting: RuntimeSettingItem): string | null {
  const lo = setting.min;
  const hi = setting.max;
  if (lo != null && hi != null) return `${lo}–${hi}`;
  if (lo != null) return `≥ ${lo}`;
  if (hi != null) return `≤ ${hi}`;
  return null;
}

function validateNumeric(setting: RuntimeSettingItem, draft: string) {
  return validateNumericSettingInput(draft, {
    min: setting.min,
    max: setting.max,
    integer: setting.value_type === 'int',
  });
}

const flagText = (value: unknown) => (value ? 'On' : 'Off');

/** A changed runtime setting: what is saved, and the value the save would write. */
interface SettingChange {
  setting: RuntimeSettingItem;
  value: boolean | number;
}

/**
 * A group of runtime settings of one type (feature flags or numbers), shown
 * read-only until Edit. The edited values are drafts until the administrator
 * confirms the list of changes; each changed setting is one request.
 */
function RuntimeSettingsSection({
  id,
  title,
  description,
  kind,
  settings,
  loading,
  emptyText,
  onSaved,
  onAfterWrite,
  onToast,
  renderExtra,
}: {
  /** Anchor for the page's section index. */
  id: string;
  title: string;
  description: string;
  kind: 'flag' | 'numeric';
  settings: RuntimeSettingItem[];
  loading: boolean;
  emptyText: string;
  onSaved: (updated: RuntimeSettingItem) => void;
  /** Called once after a save that wrote at least one setting, even partly. */
  onAfterWrite: () => void;
  onToast: (msg: string) => void;
  /** Read-only actions for a row, such as applying a quota to existing users. */
  renderExtra?: (setting: RuntimeSettingItem) => React.ReactNode;
}) {
  const confirmation = useConfirmAction();
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string | boolean>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  const draftOf = (setting: RuntimeSettingItem) =>
    drafts[setting.key] ?? (kind === 'flag' ? !!setting.value : String(setting.value ?? ''));

  // A setting is changed when its draft is valid and differs from what is saved.
  const changes = settings.flatMap((setting): SettingChange[] => {
    if (!(setting.key in drafts)) return [];
    const draft = drafts[setting.key];
    if (kind === 'flag') {
      return draft === !!setting.value ? [] : [{ setting, value: draft as boolean }];
    }
    const validated = validateNumeric(setting, String(draft));
    if (!validated.ok || validated.value === setting.value) return [];
    return [{ setting, value: validated.value as number }];
  });
  const invalid =
    kind === 'numeric'
      ? settings.filter(
          (setting) =>
            setting.key in drafts && !validateNumeric(setting, String(drafts[setting.key])).ok,
        )
      : [];

  const cancel = () => {
    if (confirmation.busy) return;
    setDrafts({});
    setSaveError(null);
    setEditing(false);
  };

  const save = () => {
    if (invalid.length > 0) {
      setSaveError(`Correct ${invalid.map((setting) => setting.key).join(', ')} before saving.`);
      return;
    }
    if (changes.length === 0) return;
    setSaveError(null);
    const pending = changes;
    const format = (value: unknown) => (kind === 'flag' ? flagText(value) : String(value));
    confirmation.request({
      title: `Save ${plural(pending.length, 'change')} to ${title}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ setting, value }) => ({
              label: settingLabel(setting.key),
              before: format(setting.value),
              after: format(value),
            }))}
          />
          {pending.length === 1 && pending[0].setting.description ? (
            <p>{pending[0].setting.description}</p>
          ) : null}
          <p>This takes effect immediately.</p>
        </>
      ),
      run: async () => {
        let wrote = false;
        const failure = await writeEach(
          pending,
          ({ setting }) => setting.key,
          async ({ setting, value }) => {
            const updated = await updateRuntimeSetting(setting.key, value);
            wrote = true;
            onSaved(updated);
            setDrafts((prev) => {
              const next = { ...prev };
              delete next[setting.key];
              return next;
            });
          },
        ).finally(() => {
          if (wrote) onAfterWrite();
        });
        setSaveError(failure);
        if (failure) return;
        setEditing(false);
        onToast(
          pending.length === 1
            ? kind === 'flag'
              ? `${pending[0].setting.key} ${pending[0].value ? 'enabled' : 'disabled'}`
              : `${pending[0].setting.key} set to ${pending[0].value}`
            : `Saved ${pending.length} settings`,
        );
      },
    });
  };

  return (
    <AdminSection
      id={id}
      title={title}
      description={description}
      actions={
        !editing && !loading && settings.length > 0 ? (
          <EditButton target={title} onClick={() => setEditing(true)} />
        ) : null
      }
    >
      {loading ? (
        <AdminLoading size="sm" />
      ) : settings.length === 0 ? (
        <AdminEmptyState title={emptyText} />
      ) : (
        <ul
          className={`${PANEL} divide-y divide-gray-100 ${editing ? 'border-gray-400 shadow-sm' : ''}`}
        >
          {settings.map((setting) => {
            const isDefault = setting.value === setting.default_value;
            const draft = draftOf(setting);
            const validated = kind === 'numeric' ? validateNumeric(setting, String(draft)) : null;
            const hint = kind === 'numeric' ? rangeHint(setting) : null;
            const name =
              kind === 'flag' ? `Toggle ${setting.key.replace(/_/g, ' ')}` : `${setting.key} value`;
            return (
              <li
                key={setting.key}
                className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-gray-900">
                      {settingLabel(setting.key)}
                    </span>
                    {!isDefault && (
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                        Modified
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">
                    {setting.description}
                    {hint && <span className="ml-1 text-gray-400">(range: {hint})</span>}
                    {kind === 'numeric' ? (
                      <span className="ml-1 text-gray-400">
                        Default: {String(setting.default_value)}
                      </span>
                    ) : null}
                  </p>
                  {editing && validated && !validated.ok && String(draft) !== '' && (
                    <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                      {validated.error}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {!editing ? (
                    <output aria-label={name} className="text-[13px] font-medium text-gray-900">
                      {kind === 'flag' ? flagText(setting.value) : String(setting.value ?? '')}
                    </output>
                  ) : kind === 'flag' ? (
                    <button
                      type="button"
                      role="switch"
                      aria-checked={draft === true}
                      aria-label={name}
                      disabled={confirmation.busy}
                      onClick={() =>
                        setDrafts((prev) => ({ ...prev, [setting.key]: !(draft === true) }))
                      }
                      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-40 ${
                        draft === true ? 'bg-gray-900' : 'bg-gray-200'
                      }`}
                    >
                      <span
                        className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                          draft === true ? 'translate-x-5' : 'translate-x-0'
                        }`}
                      />
                    </button>
                  ) : (
                    <input
                      type="number"
                      step={setting.value_type === 'int' ? 1 : 'any'}
                      min={setting.min ?? undefined}
                      max={setting.max ?? undefined}
                      value={String(draft)}
                      disabled={confirmation.busy}
                      onChange={(e) =>
                        setDrafts((prev) => ({ ...prev, [setting.key]: e.target.value }))
                      }
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          save();
                        }
                      }}
                      aria-label={name}
                      className={`${CONTROL} w-24 text-right`}
                    />
                  )}
                  {!editing && renderExtra ? renderExtra(setting) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {editing ? (
        <EditBar
          target={title}
          changeCount={changes.length}
          onCancel={cancel}
          onSave={save}
          busy={confirmation.busy}
          error={saveError}
        />
      ) : null}
      {confirmation.dialog}
    </AdminSection>
  );
}

export function SettingsTab() {
  const router = useRouter();
  const confirmation = useConfirmAction();
  const [domains, setDomains] = useState<SignupAllowedDomain[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [addingDomain, setAddingDomain] = useState(false);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState<string | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [featureFlags, setFeatureFlags] = useState<RuntimeSettingItem[]>([]);
  const [numericSettings, setNumericSettings] = useState<RuntimeSettingItem[]>([]);
  const [flagsLoading, setFlagsLoading] = useState(true);
  const [flagsError, setFlagsError] = useState<string | null>(null);

  const QUOTA_KEY_PREFIX = 'user_daily_quota_';
  const [quotaLoadingRole, setQuotaLoadingRole] = useState<Role | null>(null);

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
      setNumericSettings(
        resp.settings.filter((s) => s.value_type === 'int' || s.value_type === 'float'),
      );
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

  const replaceSetting = (setList: typeof setFeatureFlags) => (updated: RuntimeSettingItem) =>
    setList((prev) => prev.map((s) => (s.key === updated.key ? updated : s)));

  const onAddDomain = () => {
    setInputError(null);
    const validated = validateSignupDomainInput(input);
    if (!validated.ok) {
      setInputError(validated.error);
      return;
    }
    const shownDomain = validated.isWildcard ? `*.${validated.domain}` : validated.domain;
    confirmation.request({
      title: `Add ${shownDomain} to the signup allowlist?`,
      confirmLabel: 'Add domain',
      busyLabel: 'Adding…',
      cancelLabel: 'Back to editing',
      body: (
        <p className="[overflow-wrap:anywhere]">
          Future signups from <code className="rounded bg-gray-100 px-1">{shownDomain}</code>{' '}
          {validated.isWildcard ? '(any subdomain, not the bare suffix) ' : ''}are approved
          automatically.
        </p>
      ),
      run: async () => {
        await addSignupAllowedDomain(input.trim());
        setInput('');
        setAddingDomain(false);
        flashToast(
          validated.isWildcard
            ? `Added wildcard *.${validated.domain}.`
            : `Added ${validated.domain}.`,
        );
        await loadDomains();
      },
    });
  };

  const onRemove = (row: SignupAllowedDomain) => {
    const shownDomain = row.is_wildcard ? `*.${row.domain}` : row.domain;
    confirmation.request({
      title: `Remove ${shownDomain} from the signup allowlist?`,
      confirmLabel: 'Remove',
      busyLabel: 'Removing…',
      tone: 'danger',
      body: (
        <p className="[overflow-wrap:anywhere]">
          Future signups from <code className="rounded bg-gray-100 px-1">{shownDomain}</code> will
          require admin approval. Existing users keep their current status.
        </p>
      ),
      run: async () => {
        await removeSignupAllowedDomain(row.domain, row.is_wildcard);
        flashToast(
          row.is_wildcard ? `Removed wildcard *.${row.domain}.` : `Removed ${row.domain}.`,
        );
        await loadDomains();
      },
    });
  };

  // The preview is a read; applying is the write, confirmed with what the preview found.
  const onClickApply = async (role: Role) => {
    setQuotaLoadingRole(role);
    let preview: RoleQuotaPreview;
    try {
      preview = await previewRoleQuotaApply(role);
    } catch (e) {
      flashToast(`Preview failed: ${getErrorMessage(e)}`);
      return;
    } finally {
      setQuotaLoadingRole(null);
    }
    confirmation.request({
      title: `Apply the ${preview.role} quota to existing keys?`,
      confirmLabel: 'Apply quota',
      busyLabel: 'Applying…',
      tone: 'danger',
      body: (
        <p>
          This sets <code>quota_daily_cost_usd = ${preview.quota.toString()}</code> on{' '}
          <strong>{preview.keys_affected}</strong> active API keys belonging to{' '}
          <strong>{preview.users_affected}</strong> users with role <code>{preview.role}</code>.
          Custom per-key overrides will be lost.
        </p>
      ),
      run: async () => {
        const res = await applyRoleQuota(preview.role);
        flashToast(`Updated ${res.keys_updated} keys for role ${preview.role} to $${res.quota}`);
      },
    });
  };

  return (
    // A section index beside full-width sections, one after another.
    <div className={`mt-6 ${INDEXED_LAYOUT}`}>
      <AdminSectionNav label="Settings sections" items={SECTIONS} />

      <div className="min-w-0">
        {/* Feedback for every section on this page. The live region is
            always there (so a screen reader announces each message) and
            takes no height: a message floats at the top right of the
            sections, pinned to the top of the window while they scroll,
            without pushing them down, and lets clicks through to whatever
            is under it. */}
        <div className="pointer-events-none sticky top-4 z-20 h-0">
          <div role="status" className="flex justify-end">
            {toast && (
              <div
                key={toast}
                className="animate-admin-reveal max-w-md rounded-lg bg-gray-900 px-3 py-2 text-[12px] text-white shadow-lg"
              >
                {toast}
              </div>
            )}
          </div>
        </div>

        <div className="space-y-6 [&>*+*]:border-t [&>*+*]:border-gray-200 [&>*+*]:pt-6">
          {flagsError && (
            <AdminNotice onDismiss={() => setFlagsError(null)}>{flagsError}</AdminNotice>
          )}

          <RuntimeSettingsSection
            id="settings-feature-flags"
            title="Feature Flags"
            description="Toggle runtime features without restarting the server. Changes take effect immediately."
            kind="flag"
            settings={featureFlags}
            loading={flagsLoading}
            emptyText="No feature flags available."
            onSaved={replaceSetting(setFeatureFlags)}
            onAfterWrite={refreshSiteConfig}
            onToast={flashToast}
          />

          <RuntimeSettingsSection
            id="settings-numeric"
            title="Numeric Settings"
            description="Adjust numeric runtime knobs (e.g. per-user concurrency caps). Changes take effect immediately."
            kind="numeric"
            settings={numericSettings}
            loading={flagsLoading}
            emptyText="No numeric settings available."
            onSaved={replaceSetting(setNumericSettings)}
            onAfterWrite={refreshSiteConfig}
            onToast={flashToast}
            renderExtra={(setting) =>
              setting.key.startsWith(QUOTA_KEY_PREFIX) ? (
                <button
                  type="button"
                  onClick={() =>
                    void onClickApply(setting.key.slice(QUOTA_KEY_PREFIX.length) as Role)
                  }
                  disabled={quotaLoadingRole !== null || confirmation.busy}
                  className={BUTTON_SECONDARY}
                >
                  {quotaLoadingRole === setting.key.slice(QUOTA_KEY_PREFIX.length)
                    ? 'Loading...'
                    : 'Apply to existing users'}
                </button>
              ) : null
            }
          />

          <div id="settings-alert-notifications" className="scroll-mt-4 focus:outline-none">
            <AlertSnoozeSection onToast={flashToast} />
          </div>

          <div id="settings-alert-types" className="scroll-mt-4 focus:outline-none">
            <AlertMutesSection onToast={flashToast} />
          </div>

          <div id="settings-usage-insights" className="scroll-mt-4 focus:outline-none">
            <UsageInsightsSettingsSection onToast={flashToast} />
          </div>

          <div id="settings-model-visibility" className="scroll-mt-4 focus:outline-none">
            <ModelVisibilitySection onToast={flashToast} />
          </div>

          <div id="settings-model-concurrency" className="scroll-mt-4 focus:outline-none">
            <ModelConcurrencySection onToast={flashToast} />
          </div>

          <AdminSection
            id="settings-signup-policy"
            title="Signup Policy"
            description="Signups from listed domains auto-approve. Other domains require admin approval. Empty list = all signups auto-approve."
            actions={
              addingDomain ? null : (
                <button
                  type="button"
                  onClick={() => setAddingDomain(true)}
                  className={BUTTON_SECONDARY}
                >
                  Add domain
                </button>
              )
            }
          >
            {/* Add form: opened explicitly, reviewed before anything is written. */}
            {addingDomain ? (
              <form
                className="mb-4 flex max-w-xl flex-col gap-1.5"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!confirmation.busy) onAddDomain();
                }}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    type="text"
                    value={input}
                    onChange={(e) => {
                      setInput(e.target.value);
                      setInputError(null);
                    }}
                    placeholder="example.com or *.example.com"
                    className={`${CONTROL} min-w-[12rem] flex-1`}
                    disabled={confirmation.busy}
                    aria-label="Domain to add"
                    autoFocus
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setAddingDomain(false);
                      setInput('');
                      setInputError(null);
                    }}
                    disabled={confirmation.busy}
                    className={BUTTON_GHOST}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={confirmation.busy || !input.trim()}
                    className={BUTTON_PRIMARY}
                  >
                    Review and add
                  </button>
                </div>
                <p className="text-[11px] text-gray-400">
                  Enter <code className="rounded bg-gray-100 px-1">example.com</code> for an exact
                  match or <code className="rounded bg-gray-100 px-1">*.example.com</code> to allow
                  any subdomain. <code className="rounded bg-gray-100 px-1">*.example.com</code>{' '}
                  does not match the bare suffix.
                </p>
                {inputError && (
                  <p className="text-[12px] text-red-600" role="alert">
                    {inputError}
                  </p>
                )}
              </form>
            ) : null}

            {error && (
              <div className="mb-3">
                <AdminNotice onDismiss={() => setError(null)}>{error}</AdminNotice>
              </div>
            )}

            {loading ? (
              <AdminLoading size="sm" />
            ) : domains.length === 0 ? (
              <AdminEmptyState title="No allowed domains. All signups auto-approve." />
            ) : (
              <div className={`${PANEL} relative overflow-x-auto`}>
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
                              onClick={() => onRemove(d)}
                              disabled={confirmation.busy}
                              aria-label={`Remove ${d.is_wildcard ? `*.${d.domain}` : d.domain}`}
                              className={ROW_ACTION_DANGER}
                            >
                              Remove
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

      {confirmation.dialog}
    </div>
  );
}
