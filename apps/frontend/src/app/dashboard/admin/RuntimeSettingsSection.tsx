'use client';

import { useState } from 'react';
import { RuntimeSettingItem, updateRuntimeSetting } from '@/lib/api/admin';
import { AdminLoading, AdminSection } from '@/components/features/admin/AdminPrimitives';
import { CONTROL, PANEL } from '@/components/features/admin/adminStyles';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import { moneyCurrency } from '@/lib/utils/money';

import { validateNumericSettingInput } from './numericSettingValidation';
import { useReportUnsaved } from './useReportUnsaved';

const ANALYTICS_SETTING_LABELS: Readonly<Record<string, string>> = {
  analytics_token_usage_enabled: 'Token Usage',
  analytics_users_and_requests_enabled: 'Users and Requests',
  analytics_performance_metrics_enabled: 'Performance Metrics',
  analytics_growth_rate_enabled: 'Growth Rate',
};

function settingLabel(key: string): string {
  if (ANALYTICS_SETTING_LABELS[key]) return ANALYTICS_SETTING_LABELS[key];
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

const QUOTA_KEY_PREFIX = 'user_daily_quota_';

const isQuotaSetting = (setting: RuntimeSettingItem) => setting.key.startsWith(QUOTA_KEY_PREFIX);

/**
 * A role quota is edited as its native amount, in the setting's currency. The
 * legacy `value` is a USD figure, so it stands in only when that currency is USD.
 */
function settingValue(setting: RuntimeSettingItem): unknown {
  if (!isQuotaSetting(setting)) return setting.value;
  return setting.amount ?? (moneyCurrency(setting) === 'USD' ? setting.value : null);
}

function settingDefault(setting: RuntimeSettingItem): unknown {
  if (!isQuotaSetting(setting)) return setting.default_value;
  return (
    setting.default_amount ?? (moneyCurrency(setting) === 'USD' ? setting.default_value : null)
  );
}

function settingBound(
  setting: RuntimeSettingItem,
  bound: 'min' | 'max',
): number | null | undefined {
  if (!isQuotaSetting(setting)) return setting[bound];
  return (
    setting[`${bound}_amount`] ?? (moneyCurrency(setting) === 'USD' ? setting[bound] : undefined)
  );
}

/** A role quota in another currency than USD, whose registered description speaks of USD. */
const isNativeQuota = (setting: RuntimeSettingItem) =>
  isQuotaSetting(setting) && moneyCurrency(setting) !== 'USD';

/** The row's description; a non-USD role quota names the currency its amount is in. */
function settingDescription(setting: RuntimeSettingItem): string {
  return isNativeQuota(setting) ? `Daily quota (${moneyCurrency(setting)})` : setting.description;
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

/**
 * A group of runtime settings, switches and numbers alike, edited in place.
 * The edited values are drafts until Save; each changed setting is one
 * request. Nothing once loaded with no settings to show: a deployment may not
 * have every setting a group lists.
 */
export function RuntimeSettingsSection({
  id,
  title,
  description,
  settings,
  loading,
  onSaved,
  onAfterWrite,
  onToast,
  renderExtra,
  onUnsavedChange,
}: {
  /** Anchor of the section. */
  id: string;
  title: string;
  description: string;
  settings: RuntimeSettingItem[];
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

  const save = async () => {
    // Enter in a field reaches here too; the bar already says what to correct.
    if (blocked || changes.length === 0) return;
    setSaveError(null);
    const pending = changes;
    const failure = await writes.perform({
      run: async () => {
        let wrote = false;
        await writeEach(
          pending,
          ({ setting }) => setting.key,
          async ({ setting, value }) => {
            // A role quota is sent with the currency its amount was read in.
            const updated = isQuotaSetting(setting)
              ? await updateRuntimeSetting(setting.key, value, moneyCurrency(setting))
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
        onToast(
          pending.length === 1
            ? isFlag(pending[0].setting)
              ? `${pending[0].setting.key} ${pending[0].value ? 'enabled' : 'disabled'}`
              : `${pending[0].setting.key} set to ${pending[0].value}`
            : `Saved ${pending.length} settings`,
        );
      },
    });
    setSaveError(failure);
  };

  if (!loading && settings.length === 0) return null;

  return (
    <AdminSection id={id} title={title} description={description}>
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
                    {settingDescription(setting)}
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
                      min={settingBound(setting, 'min') ?? undefined}
                      max={settingBound(setting, 'max') ?? undefined}
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
    </AdminSection>
  );
}
