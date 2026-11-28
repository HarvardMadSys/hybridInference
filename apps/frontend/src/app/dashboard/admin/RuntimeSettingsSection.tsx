'use client';

import { useRef, useState } from 'react';
import {
  type LedgerCurrency,
  RuntimeSettingItem,
  updateRuntimeSetting,
  updateRuntimeSettings,
} from '@/lib/api/admin';
import { AdminLoading, AdminSection } from '@/components/features/admin/AdminPrimitives';
import { CONTROL, PANEL } from '@/components/features/admin/adminStyles';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import {
  ReauthPassword,
  useConfirmsWithoutPassword,
} from '@/components/features/admin/confirm/ReauthPassword';
import { useSpendAdminSso } from '@/components/features/admin/confirm/adminSsoReauth';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';

import { validateNumericSettingInput } from './numericSettingValidation';
import { useReportUnsaved } from './useReportUnsaved';

const ANALYTICS_SETTING_LABELS: Readonly<Record<string, string>> = {
  analytics_token_usage_enabled: 'Token Usage',
  analytics_users_and_requests_enabled: 'Users and Requests',
  analytics_performance_metrics_enabled: 'Performance Metrics',
  analytics_growth_rate_enabled: 'Growth Rate',
};

/** Plain labels for the known switches; anything new falls back to its key in words. */
const SETTING_LABELS: Readonly<Record<string, string>> = {
  ...ANALYTICS_SETTING_LABELS,
  user_auth_enabled: 'Require API key for inference',
  signup_enabled: 'Allow new sign-ups',
  signup_require_email_verification: 'Require email verification for sign-ups',
  signup_admin_notify_enabled: 'Email admins about new sign-ups',
  log_full_payload: 'Log full request payloads',
  log_rejected_requests: 'Log rejected requests',
  log_synthetic_probes: 'Log synthetic probes',
  force_chat_completions_streaming: 'Stream chat completions upstream',
  reasoning_small_call_reroute_enabled: 'Reroute small reasoning calls',
  auto_mode_enabled: 'Enable auto models',
};

function roleLabel(key: string, prefix: string): string {
  return key
    .slice(prefix.length)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function settingLabel(key: string): string {
  if (SETTING_LABELS[key]) return SETTING_LABELS[key];
  if (key.startsWith('user_concurrency_'))
    return `${roleLabel(key, 'user_concurrency_')} concurrency limit`;
  if (key.startsWith('user_daily_quota_'))
    return `${roleLabel(key, 'user_daily_quota_')} daily quota`;
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

const isQuotaSetting = (setting: RuntimeSettingItem) => setting.key.startsWith('user_daily_quota_');

/** The unit a role quota is edited and sent in: the ledger's, USD from an older gateway. */
const quotaCurrency = (setting: RuntimeSettingItem): LedgerCurrency =>
  setting.currency === 'CNY' ? 'CNY' : 'USD';

/**
 * What a role quota's field shows and is compared with: its amount in the
 * ledger's currency. `value` and the bounds are USD figures, so they stand in
 * only on a USD ledger; on another one a missing amount shows nothing rather
 * than a USD number under the ledger's unit.
 */
function inLedger(
  setting: RuntimeSettingItem,
  usd: 'value' | 'default_value' | 'min' | 'max',
  native: 'amount' | 'default_amount' | 'min_amount' | 'max_amount',
): unknown {
  if (!isQuotaSetting(setting)) return setting[usd];
  return setting[native] ?? (quotaCurrency(setting) === 'USD' ? setting[usd] : null);
}

const settingValue = (setting: RuntimeSettingItem) => inLedger(setting, 'value', 'amount');
const settingDefault = (setting: RuntimeSettingItem) =>
  inLedger(setting, 'default_value', 'default_amount');
const settingBound = (setting: RuntimeSettingItem, bound: 'min' | 'max') =>
  (inLedger(setting, bound, `${bound}_amount`) as number | null | undefined) ?? undefined;

/** The currency the role quotas among *pending* are sent in, when there are any. */
function pendingCurrency(pending: SettingChange[]): LedgerCurrency | undefined {
  const quota = pending.find(({ setting }) => isQuotaSetting(setting));
  return quota ? quotaCurrency(quota.setting) : undefined;
}

function rangeHint(setting: RuntimeSettingItem): string | null {
  const lo = settingBound(setting, 'min');
  const hi = settingBound(setting, 'max');
  if (lo != null && hi != null) return `${lo}–${hi}`;
  if (lo != null) return `≥ ${lo}`;
  if (hi != null) return `≤ ${hi}`;
  return null;
}

function validateNumeric(setting: RuntimeSettingItem, draft: string) {
  return validateNumericSettingInput(draft, {
    min: settingBound(setting, 'min'),
    max: settingBound(setting, 'max'),
    integer: setting.value_type === 'int',
  });
}

/** A changed runtime setting: what is saved, and the value the save would write. */
interface SettingChange {
  setting: RuntimeSettingItem;
  value: boolean | number;
}

/** Whether a setting is a switch rather than a number. */
function isFlag(setting: RuntimeSettingItem): boolean {
  return setting.value_type === 'bool';
}

/** What a save says once it is done. */
function savedMessage(pending: SettingChange[]): string {
  if (pending.length !== 1) return `Saved ${pending.length} settings`;
  const [{ setting, value }] = pending;
  return isFlag(setting)
    ? `${setting.key} ${value ? 'enabled' : 'disabled'}`
    : `${setting.key} set to ${value}`;
}

/**
 * A group of runtime settings, switches and numbers alike, edited in place.
 * The edited values are drafts until Save; each changed setting is one
 * request. Nothing once loaded with no settings to show: a deployment may not
 * have every setting a group lists.
 *
 * A save that changes a `security_relevant` setting -- who may sign in or sign
 * up, whether requests need a key, what is logged of them, what each account
 * may spend and run -- is confirmed first, as the gateway requires: by a code,
 * the administrator's password, or a fresh single sign-on (`ReauthPassword`).
 * Its changes are then written together, behind that one confirmation.
 */
export function RuntimeSettingsSection({
  id,
  title,
  description,
  tag,
  settings,
  loading,
  onSaved,
  onAfterWrite,
  onToast,
  renderExtra,
  onUnsavedChange,
  visibleKeys,
}: {
  /** Anchor of the section. */
  id: string;
  title: string;
  description: string;
  /** Shown beside the title: where the section lives, while a filter lists every tab's. */
  tag?: React.ReactNode;
  settings: RuntimeSettingItem[];
  /** While searching: only these keys' rows are shown; the rest stay mounted, drafts intact. */
  visibleKeys?: ReadonlySet<string>;
  loading: boolean;
  onSaved: (updated: RuntimeSettingItem) => void;
  /** Called once after a save that wrote at least one setting, even partly. */
  onAfterWrite: () => void;
  onToast: (msg: string) => void;
  /**
   * Actions on a row's saved value, such as applying a quota to existing users.
   * `unsaved` while the row has a draft, which the action would not act on.
   */
  renderExtra?: (setting: RuntimeSettingItem, unsaved: boolean) => React.ReactNode;
  /** Called with the number of unsaved changes whenever it changes. */
  onUnsavedChange?: (count: number) => void;
}) {
  const writes = useConfirmAction();
  const [drafts, setDrafts] = useState<Record<string, string | boolean>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  // A guarded save is confirmed in a dialog; the password typed there, if any.
  const password = useRef('');
  const withoutPassword = useConfirmsWithoutPassword();
  const spendSso = useSpendAdminSso();

  const savedDraft = (setting: RuntimeSettingItem) =>
    isFlag(setting) ? !!setting.value : String(settingValue(setting) ?? '');
  const draftOf = (setting: RuntimeSettingItem) => drafts[setting.key] ?? savedDraft(setting);

  // A value set back to what is saved is no draft, so a later refresh of the
  // saved value shows through instead of being masked by a stale copy.
  const changeDraft = (setting: RuntimeSettingItem, value: string | boolean) =>
    setDrafts((prev) => {
      const next = { ...prev };
      if (value === savedDraft(setting)) delete next[setting.key];
      else next[setting.key] = value;
      return next;
    });

  // A setting is changed when its draft is valid and differs from what is saved.
  const changes = settings.flatMap((setting): SettingChange[] => {
    if (!(setting.key in drafts)) return [];
    const draft = drafts[setting.key];
    if (isFlag(setting)) {
      return draft === !!setting.value ? [] : [{ setting, value: draft as boolean }];
    }
    const validated = validateNumeric(setting, String(draft));
    if (!validated.ok || validated.value === settingValue(setting)) return [];
    return [{ setting, value: validated.value as number }];
  });
  const invalid = settings.filter(
    (setting) =>
      !isFlag(setting) &&
      setting.key in drafts &&
      !validateNumeric(setting, String(drafts[setting.key])).ok,
  );
  // An invalid draft is unsaved too: it keeps the bar up, with Save held back.
  const unsavedKeys = new Set([
    ...changes.map(({ setting }) => setting.key),
    ...invalid.map((setting) => setting.key),
  ]);
  useReportUnsaved(unsavedKeys.size, onUnsavedChange);
  const blocked =
    invalid.length > 0
      ? `Correct ${invalid.map((setting) => setting.key).join(', ')} before saving.`
      : null;

  const cancel = () => {
    if (writes.busy) return;
    setDrafts({});
    setSaveError(null);
  };

  const dropDrafts = (keys: readonly string[]) =>
    setDrafts((prev) => {
      const next = { ...prev };
      for (const key of keys) delete next[key];
      return next;
    });

  /** Ask for the confirmation a guarded save needs, then write it all at once. */
  const confirmGuarded = (pending: SettingChange[], guarded: string[]) => {
    password.current = '';
    writes.request({
      key: id,
      title: guarded.length === 1 ? `Change ${guarded[0]}?` : 'Change these settings?',
      confirmLabel: 'Save changes',
      busyLabel: 'Saving…',
      body: (
        <div className="space-y-2">
          <p>
            {guarded.length === 1 ? 'This setting decides' : 'These settings decide'} who may sign
            in or sign up, whether requests need a key, what is logged of them, or what each account
            may spend and run:{' '}
            {guarded.map((key, index) => (
              <span key={key}>
                {index > 0 ? ', ' : null}
                <code className="text-[12px]">{key}</code>
              </span>
            ))}
            .{withoutPassword ? null : ' Confirm the change with your password.'}
          </p>
          <ReauthPassword valueRef={password} label="Your password" />
        </div>
      ),
      run: async () => {
        const given = password.current;
        if (!given && !withoutPassword) {
          throw new Error('Enter your password to save these settings.');
        }
        // A refusal (a wrong password, a code owed) stays in the dialog.
        const values = Object.fromEntries(
          pending.map(({ setting, value }) => [setting.key, value]),
        );
        // Role quotas are sent in the unit their fields were read in.
        const currency = pendingCurrency(pending);
        const updated = await spendSso(() =>
          currency
            ? updateRuntimeSettings(values, given || undefined, currency)
            : updateRuntimeSettings(values, given || undefined),
        );
        password.current = '';
        for (const item of updated) onSaved(item);
        dropDrafts(pending.map(({ setting }) => setting.key));
        onAfterWrite();
        onToast(savedMessage(pending));
      },
    });
  };

  const save = async () => {
    // Enter in a field reaches here too; the bar already says what to correct.
    if (blocked || changes.length === 0) return;
    setSaveError(null);
    const pending = changes;
    const guarded = pending
      .filter(({ setting }) => setting.security_relevant === true)
      .map(({ setting }) => setting.key);
    if (guarded.length > 0) {
      confirmGuarded(pending, guarded);
      return;
    }
    const failure = await writes.perform({
      run: async () => {
        let wrote = false;
        await writeEach(
          pending,
          ({ setting }) => setting.key,
          async ({ setting, value }) => {
            // A role quota is sent in the unit its field was read in.
            const updated = isQuotaSetting(setting)
              ? await updateRuntimeSetting(setting.key, value, undefined, quotaCurrency(setting))
              : await updateRuntimeSetting(setting.key, value);
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
        onToast(savedMessage(pending));
      },
    });
    setSaveError(failure);
  };

  if (!loading && settings.length === 0) return null;

  return (
    <AdminSection
      id={id}
      title={title}
      description={description}
      badge={
        tag ? (
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600">
            {tag}
          </span>
        ) : undefined
      }
    >
      {loading ? (
        <AdminLoading size="sm" />
      ) : (
        <ul
          className={`${PANEL} divide-y divide-gray-100 ${unsavedKeys.size > 0 ? 'border-gray-400 shadow-sm' : ''}`}
        >
          {settings.map((setting) => {
            const flag = isFlag(setting);
            const isDefault = settingValue(setting) === settingDefault(setting);
            const draft = draftOf(setting);
            const validated = flag ? null : validateNumeric(setting, String(draft));
            const hint = flag ? null : rangeHint(setting);
            const name = flag
              ? `Toggle ${ANALYTICS_SETTING_LABELS[setting.key] ?? setting.key.replace(/_/g, ' ')}`
              : `${setting.key} value`;
            return (
              <li
                key={setting.key}
                // A link to `#<key>` lands on the setting.
                data-runtime-key={setting.key}
                className={`${visibleKeys && !visibleKeys.has(setting.key) ? 'hidden' : 'flex'} flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6`}
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
                    {!flag ? (
                      <span className="ml-1 text-gray-400">
                        Default: {String(settingDefault(setting) ?? '—')}
                      </span>
                    ) : null}
                  </p>
                  {setting.key in drafts && validated && !validated.ok && String(draft) !== '' && (
                    <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                      {validated.error}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {flag ? (
                    <button
                      type="button"
                      role="switch"
                      aria-checked={draft === true}
                      aria-label={name}
                      disabled={writes.busy}
                      onClick={() => changeDraft(setting, !(draft === true))}
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
                      min={settingBound(setting, 'min')}
                      max={settingBound(setting, 'max')}
                      value={String(draft)}
                      disabled={writes.busy}
                      onChange={(e) => changeDraft(setting, e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          void save();
                        }
                      }}
                      aria-label={name}
                      className={`${CONTROL} w-24 text-right`}
                    />
                  )}
                  {renderExtra ? renderExtra(setting, unsavedKeys.has(setting.key)) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <EditBar
        target={title}
        changeCount={unsavedKeys.size}
        onCancel={cancel}
        onSave={() => void save()}
        busy={writes.busy}
        error={saveError}
        blocked={blocked}
        inPlace
      />
      {writes.dialog}
    </AdminSection>
  );
}
