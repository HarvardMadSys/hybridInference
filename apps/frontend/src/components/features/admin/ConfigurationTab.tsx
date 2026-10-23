'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import {
  ConfigEntryField,
  NOT_A_LOGIN_FIELD,
} from '@/components/features/configuration/ConfigEntryField';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CARD,
  CHECKBOX,
  CONTROL,
  FORM_LABEL,
  NOTICE_ACTION,
  TEXT_FIELD,
} from '@/components/features/admin/adminStyles';
import {
  AlertNotificationFields,
  type AlertTestResult,
} from '@/components/features/configuration/AlertNotificationFields';
import {
  ALERT_WEBHOOK_KEYS,
  alertTestFailureMessage,
  alertWebhookEntries,
  alertWebhookError,
  buildAlertChannelPatch,
  isAlertWebhook,
  savedAlertChannel,
  selectedWebhookEntry,
  type AlertChannelChoice,
} from '@/components/features/configuration/alertNotificationDrafts';
import { KeyList } from '@/components/features/configuration/KeyList';
import { CONFIGURATION_TAB_PATH } from '@/components/features/configuration/paths';
import {
  ManualRestartHint,
  RestartBackendButton,
} from '@/components/features/configuration/RestartBackend';
import {
  CUSTOM_KEY_PATTERN,
  buildPatch,
  countDirty,
  errorForKey,
  groupEntries,
  isDirty,
  looksSecret,
  matchesQuery,
  withoutKeys,
  type ConfigDraft,
  type ConfigDrafts,
  type ConfigGroup,
} from '@/components/features/configuration/configDrafts';
import {
  getConfig,
  markConfigSecret,
  patchConfig,
  resetConfigKey,
  testAlertNotification,
  type ConfigEntry,
  type ConfigResponse,
} from '@/lib/api/config';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * Admin → Configuration: every database-backed setting, grouped by category.
 *
 * Each category saves its own changes as one batch (the backend validates a
 * batch together, so related settings can change in one save). After any
 * write the page asks the server components to re-render, which re-reads the
 * public site configuration and so updates the "required settings are
 * missing" banner.
 */
export function ConfigurationTab({ initialMissingOnly = false }: { initialMissingOnly?: boolean }) {
  const toast = useAccountToast();
  const router = useRouter();
  const searchId = useId();
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<ConfigDrafts>({});
  const [entryErrors, setEntryErrors] = useState<Record<string, string>>({});
  const [groupErrors, setGroupErrors] = useState<Record<string, string>>({});
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [missingOnly, setMissingOnly] = useState(initialMissingOnly);
  const [resetTarget, setResetTarget] = useState<ConfigEntry | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);
  // The key alone: the entry still holds the value that is about to be hidden.
  const [markSecretKey, setMarkSecretKey] = useState<string | null>(null);
  const [markingSecret, setMarkingSecret] = useState(false);
  const [markSecretError, setMarkSecretError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [alertChannelDraft, setAlertChannelDraft] = useState<AlertChannelChoice | null>(null);
  const [testingAlert, setTestingAlert] = useState(false);
  const alertTestInFlight = useRef(false);
  const [alertTestResult, setAlertTestResult] = useState<AlertTestResult | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setConfig(await getConfig());
    } catch (e) {
      setLoadError(
        e instanceof APIError && e.statusCode === 404
          ? 'This backend does not support database-backed configuration. Upgrade it to edit settings here.'
          : getErrorMessage(e),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Take the server's state after a write; the written keys have no draft left. */
  const applyResponse = useCallback(
    (next: ConfigResponse, writtenKeys: readonly string[]) => {
      setConfig(next);
      setDrafts((prev) => withoutKeys(prev, writtenKeys));
      setEntryErrors((prev) => withoutKeys(prev, writtenKeys));
      if (writtenKeys.some(isAlertWebhook)) setAlertTestResult(null);
      // Re-render the server components: the banner reads site-config there.
      router.refresh();
    },
    [router],
  );

  const onDraftChange = useCallback((key: string, draft: ConfigDraft | undefined) => {
    setDrafts((prev) => {
      const next = { ...prev };
      if (draft) next[key] = draft;
      else delete next[key];
      return next;
    });
    setEntryErrors((prev) => withoutKeys(prev, [key]));
    if (isAlertWebhook(key)) setAlertTestResult(null);
  }, []);

  const webhookEntries = config ? alertWebhookEntries(config.entries) : null;
  const alertChannel =
    alertChannelDraft ?? (webhookEntries ? savedAlertChannel(webhookEntries) : 'none');

  const changeAlertChannel = (channel: AlertChannelChoice) => {
    if (!webhookEntries || alertTestInFlight.current) return;
    setAlertChannelDraft(channel === savedAlertChannel(webhookEntries) ? null : channel);
    setDrafts((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    setEntryErrors((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    setGroupErrors((prev) => withoutKeys(prev, [webhookEntries.slack.category]));
    setAlertTestResult(null);
  };

  const sendAlertTest = async () => {
    if (!webhookEntries || alertChannel === 'none' || alertTestInFlight.current) return;
    const entry = selectedWebhookEntry(webhookEntries, alertChannel);
    if (!entry || alertWebhookError(entry, drafts)) return;
    const draft = drafts[entry.key];
    alertTestInFlight.current = true;
    setTestingAlert(true);
    setAlertTestResult(null);
    try {
      const result = await testAlertNotification({
        channel: alertChannel,
        ...(draft?.kind === 'replace' ? { webhook_url: draft.value.trim() } : {}),
      });
      setAlertTestResult({ sent: true, testId: result.test_id });
    } catch (e) {
      setAlertTestResult({ sent: false, message: alertTestFailureMessage(e) });
    } finally {
      alertTestInFlight.current = false;
      setTestingAlert(false);
    }
  };

  const groups = useMemo(
    () => (config ? groupEntries(config.categories, config.entries) : []),
    [config],
  );

  const visibleGroups = useMemo(
    () =>
      groups
        .map((group) => ({
          ...group,
          entries: group.entries.filter(
            (entry) =>
              (!missingOnly || entry.missing) &&
              (matchesQuery(entry, query, group.label) ||
                (isAlertWebhook(entry.key) &&
                  'notification channel'.includes(query.trim().toLowerCase()))),
          ),
        }))
        .filter((group) => group.entries.length > 0),
    [groups, missingOnly, query],
  );

  const saveGroup = async (group: ConfigGroup) => {
    // The whole category, including entries a filter is hiding: a change the
    // administrator made stays part of the save it was made for.
    const channelEntries = alertWebhookEntries(group.entries);
    if (channelEntries && alertTestInFlight.current) return;
    const { values, errors } = buildPatch(
      channelEntries ? group.entries.filter((entry) => !isAlertWebhook(entry.key)) : group.entries,
      drafts,
    );
    if (channelEntries) {
      const channelPatch = buildAlertChannelPatch(
        channelEntries,
        drafts,
        alertChannel,
        alertChannelDraft !== null,
      );
      Object.assign(values, channelPatch.values);
      Object.assign(errors, channelPatch.errors);
    }
    if (Object.keys(errors).length > 0) {
      setEntryErrors((prev) => ({ ...prev, ...errors }));
      setGroupErrors((prev) => ({
        ...prev,
        [group.id]: 'Some values are not valid. Correct them and save again.',
      }));
      return;
    }
    const keys = Object.keys(values);
    if (keys.length === 0) return;

    setSavingGroup(group.id);
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
    try {
      const next = await patchConfig({ values });
      applyResponse(next, keys);
      if (channelEntries) setAlertChannelDraft(null);
      toast.success(keys.length === 1 ? `Saved ${keys[0]}.` : `Saved ${keys.length} settings.`);
    } catch (e) {
      const message = getErrorMessage(e);
      const keyed = errorForKey(message, keys);
      if (keyed) setEntryErrors((prev) => ({ ...prev, [keyed.key]: keyed.reason }));
      setGroupErrors((prev) => ({ ...prev, [group.id]: message }));
    } finally {
      setSavingGroup(null);
    }
  };

  const discardGroup = (group: ConfigGroup) => {
    if (alertWebhookEntries(group.entries)) {
      if (alertTestInFlight.current) return;
      setAlertChannelDraft(null);
      setAlertTestResult(null);
    }
    const keys = group.entries.map((entry) => entry.key);
    setDrafts((prev) => withoutKeys(prev, keys));
    setEntryErrors((prev) => withoutKeys(prev, keys));
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
  };

  const requestReset = (target: ConfigEntry) => {
    setResetError(null);
    setResetTarget(target);
  };

  const confirmReset = async () => {
    if (!resetTarget) return;
    const { key, custom } = resetTarget;
    setResetting(true);
    setResetError(null);
    try {
      const next = await resetConfigKey(key);
      applyResponse(next, [key]);
      if (isAlertWebhook(key)) {
        // The channel is inferred from which webhooks are set, and a reset can
        // change that: discard a pending choice rather than show it as saved.
        setAlertChannelDraft(null);
        setAlertTestResult(null);
      }
      setResetTarget(null);
      toast.success(custom ? `Deleted ${key}.` : `Reset ${key}.`);
    } catch (e) {
      setResetError(getErrorMessage(e));
    } finally {
      setResetting(false);
    }
  };

  const confirmMarkSecret = async () => {
    if (!markSecretKey) return;
    const key = markSecretKey;
    setMarkingSecret(true);
    setMarkSecretError(null);
    try {
      const next = await markConfigSecret(key);
      // Claim nothing the response does not show: a backend that ignored the
      // flag would answer with the variable unchanged.
      if (!next.entries.some((candidate) => candidate.key === key && candidate.secret)) {
        setMarkSecretError(`The backend answered, but ${key} is still not secret.`);
        return;
      }
      // Drops the key's draft and errors too: whatever the field held is the
      // plaintext the console no longer shows.
      applyResponse(next, [key]);
      const category = next.entries.find((candidate) => candidate.key === key)?.category;
      if (category !== undefined) setGroupErrors((prev) => withoutKeys(prev, [category]));
      setMarkSecretKey(null);
      toast.success(`${key} is now secret.`);
    } catch (e) {
      setMarkSecretError(getErrorMessage(e));
    } finally {
      setMarkingSecret(false);
    }
  };

  if (loading && !config) {
    return (
      <div className="mt-6">
        <AdminLoading />
      </div>
    );
  }

  if (!config) {
    return (
      <div className="mt-6">
        <AdminNotice
          role="alert"
          action={
            <button type="button" onClick={() => void load()} className={NOTICE_ACTION}>
              Retry
            </button>
          }
        >
          {loadError ?? 'Configuration could not be loaded.'}
        </AdminNotice>
      </div>
    );
  }

  const existingKeys = new Set(config.entries.map((entry) => entry.key));
  const markSecretTarget = markSecretKey
    ? config.entries.find((entry) => entry.key === markSecretKey)
    : undefined;

  return (
    <div className="mt-6 space-y-4">
      {config.pending_restart.length > 0 ? (
        <section
          aria-label="Restart pending"
          className="rounded-xl border border-amber-200 bg-amber-50 p-4"
        >
          <h2 className="text-[13px] font-semibold text-amber-900">Restart pending</h2>
          <p className="mt-1 text-[12px] text-amber-800">
            {config.pending_restart.length === 1 ? 'This setting was' : 'These settings were'} saved
            but apply only after the backend restarts: <KeyList keys={config.pending_restart} />.
          </p>
          <div className="mt-3">
            {config.restart_supported ? (
              <RestartBackendButton onRestarted={() => navigateTo(CONFIGURATION_TAB_PATH)} />
            ) : (
              <ManualRestartHint />
            )}
          </div>
        </section>
      ) : null}

      {config.missing.length > 0 ? (
        <div
          className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[12px] text-red-800"
          role="status"
        >
          <span className="font-semibold">
            {plural(config.missing.length, 'required setting')}{' '}
            {config.missing.length === 1 ? 'is' : 'are'} missing:
          </span>{' '}
          <KeyList keys={config.missing} />.
          {!missingOnly ? (
            <button
              type="button"
              onClick={() => setMissingOnly(true)}
              className="ml-2 font-medium underline underline-offset-2"
            >
              Show only these
            </button>
          ) : null}
        </div>
      ) : null}

      <div className={`${CARD} flex flex-wrap items-center gap-2 p-3`}>
        <label htmlFor={searchId} className="sr-only">
          Search settings
        </label>
        <div className="relative min-w-[14rem] flex-1">
          <svg
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
              clipRule="evenodd"
            />
          </svg>
          <input
            id={searchId}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by name, description or model"
            className={`${CONTROL} w-full pl-8`}
          />
        </div>
        <label className="flex h-8 items-center gap-1.5 px-1 text-[13px] text-gray-700">
          <input
            type="checkbox"
            checked={missingOnly}
            onChange={(event) => setMissingOnly(event.target.checked)}
            className={CHECKBOX}
          />
          Missing only
        </label>
        <button
          type="button"
          onClick={() => setAdding(true)}
          disabled={adding}
          className={BUTTON_SECONDARY}
        >
          Add variable
        </button>
      </div>

      {adding ? (
        <AddVariableForm
          existingKeys={existingKeys}
          onCancel={() => setAdding(false)}
          onAdded={(next, key) => {
            applyResponse(next, [key]);
            setAdding(false);
            toast.success(`Added ${key}.`);
          }}
        />
      ) : null}

      {visibleGroups.length === 0 ? (
        <AdminEmptyState
          title={
            missingOnly && config.missing.length === 0 && !query.trim()
              ? 'No required settings are missing.'
              : 'No settings match.'
          }
        />
      ) : null}

      {visibleGroups.map((visible) => {
        const group = groups.find((candidate) => candidate.id === visible.id) ?? visible;
        const channelEntries = alertWebhookEntries(group.entries);
        const selectedEntry = channelEntries
          ? selectedWebhookEntry(channelEntries, alertChannel)
          : null;
        const dirty = channelEntries
          ? countDirty(
              group.entries.filter((entry) => !isAlertWebhook(entry.key)),
              drafts,
            ) +
            Number(
              alertChannelDraft !== null ||
                (selectedEntry && isDirty(selectedEntry, drafts[selectedEntry.key])),
            )
          : countDirty(group.entries, drafts);
        const saving = savingGroup === group.id;
        const busy = saving || Boolean(channelEntries && testingAlert);
        const headingId = `${searchId}-${group.id}`;
        return (
          <section key={group.id} aria-labelledby={headingId} className={CARD}>
            <div className="border-b border-gray-100 px-5 py-4">
              <h2 id={headingId} className="text-[15px] font-semibold text-gray-900">
                {group.label}
              </h2>
              {group.description ? (
                <p className="mt-1 max-w-3xl text-[12px] text-gray-500">{group.description}</p>
              ) : null}
            </div>

            {groupErrors[group.id] ? (
              <div className="px-5 pt-4">
                <AdminNotice role="alert">{groupErrors[group.id]}</AdminNotice>
              </div>
            ) : null}

            {/* A form, so Enter saves the category, and one browsers do not
                offer to remember: a secret typed here is not a login. The
                console checks the values itself, so the browser does not. */}
            <form
              autoComplete="off"
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                if (!busy) void saveGroup(group);
              }}
            >
              <div className="divide-y divide-gray-100 px-5">
                {channelEntries && visible.entries.some((entry) => isAlertWebhook(entry.key)) ? (
                  <AlertNotificationFields
                    entries={channelEntries}
                    channel={alertChannel}
                    onChannelChange={changeAlertChannel}
                    drafts={drafts}
                    onDraftChange={onDraftChange}
                    errors={entryErrors}
                    disabled={busy}
                    testing={testingAlert}
                    testResult={alertTestResult}
                    onTest={() => void sendAlertTest()}
                    onReset={requestReset}
                  />
                ) : null}
                {visible.entries
                  .filter((entry) => !channelEntries || !isAlertWebhook(entry.key))
                  .map((entry) => (
                    <ConfigEntryField
                      key={entry.key}
                      entry={entry}
                      draft={drafts[entry.key]}
                      onChange={onDraftChange}
                      serverError={entryErrors[entry.key]}
                      onReset={requestReset}
                      layout="split"
                      onMarkSecret={(target) => {
                        setMarkSecretError(null);
                        setMarkSecretKey(target.key);
                      }}
                      disabled={busy}
                    />
                  ))}
              </div>

              {/* Stays in view at the bottom of the window while the
                  category with the changes is on screen. */}
              {dirty > 0 ? (
                <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-end gap-2 rounded-b-xl border-t border-gray-200 bg-white/95 px-5 py-3 backdrop-blur">
                  <span className="mr-auto text-[12px] font-medium text-amber-700">
                    {plural(dirty, 'unsaved change')}
                  </span>
                  <button
                    type="button"
                    onClick={() => discardGroup(group)}
                    disabled={busy}
                    className={BUTTON_GHOST}
                  >
                    Discard
                  </button>
                  <button
                    type="submit"
                    disabled={busy}
                    aria-busy={saving || undefined}
                    aria-label={`Save ${group.label}`}
                    className={BUTTON_PRIMARY}
                  >
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              ) : null}
            </form>
          </section>
        );
      })}

      {markSecretTarget ? (
        <ConfirmDialog
          title={`Mark ${markSecretTarget.key} as secret?`}
          confirmLabel="Mark as secret"
          busyLabel="Marking…"
          busy={markingSecret}
          error={markSecretError}
          onConfirm={() => void confirmMarkSecret()}
          onCancel={() => setMarkSecretKey(null)}
        >
          <p>
            The value stays as it is, but the console stops showing it: like any secret, it can only
            be replaced or cleared from then on.
          </p>
          <p className="font-medium text-gray-800">This cannot be undone from the console.</p>
          {isDirty(markSecretTarget, drafts[markSecretTarget.key]) ? (
            <p>Your unsaved change to it is discarded.</p>
          ) : null}
        </ConfirmDialog>
      ) : null}

      {resetTarget ? (
        <ConfirmDialog
          title={resetTarget.custom ? `Delete ${resetTarget.key}?` : `Reset ${resetTarget.key}?`}
          confirmLabel={resetTarget.custom ? 'Delete' : 'Reset'}
          busyLabel={resetTarget.custom ? 'Deleting…' : 'Resetting…'}
          tone="danger"
          busy={resetting}
          error={resetError}
          onConfirm={() => void confirmReset()}
          onCancel={() => setResetTarget(null)}
        >
          {resetTarget.custom ? (
            <p>
              The variable is removed from the database. Anything in models.yaml that references it
              resolves from the environment instead, if the environment sets it.
            </p>
          ) : (
            <p>
              The value stored in the database is removed, and the setting falls back to the
              environment, then to its default.
            </p>
          )}
          {resetTarget.restart_required ? (
            <p>The change applies after the backend restarts.</p>
          ) : null}
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/** A custom variable: a `${VAR}` reference in models.yaml the registry cannot see. */
function AddVariableForm({
  existingKeys,
  onAdded,
  onCancel,
}: {
  existingKeys: ReadonlySet<string>;
  onAdded: (next: ConfigResponse, key: string) => void;
  onCancel: () => void;
}) {
  const baseId = useId();
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  // The backend's rule: a name that looks like a credential's is secret
  // whatever the request says (a flag only ever adds secrecy). Any other name
  // is the administrator's choice.
  const [secretChoice, setSecretChoice] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const secretByName = looksSecret(name);
  const secret = secretByName || secretChoice;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const key = name.trim();
    if (!CUSTOM_KEY_PATTERN.test(key)) {
      setError(
        'Use an environment-variable name: capital letters, digits and underscores, starting with a letter.',
      );
      return;
    }
    if (existingKeys.has(key)) {
      setError(`${key} is already listed. Edit it in its category.`);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const next = await patchConfig({ values: { [key]: value }, secrets: { [key]: secret } });
      onAdded(next, key);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    // `autoComplete="off"` and the value field's attributes: a name and a
    // secret are not a login, and the browser must not offer to save them.
    <form
      onSubmit={(event) => void submit(event)}
      autoComplete="off"
      aria-labelledby={`${baseId}-title`}
      className={`${CARD} p-5`}
    >
      <h2 id={`${baseId}-title`} className="text-[15px] font-semibold text-gray-900">
        Add variable
      </h2>
      <p className="mt-1 text-[12px] text-gray-500">
        For a <code className="rounded bg-gray-100 px-1">{'${VAR}'}</code> reference in a
        configuration file that is not listed here.
      </p>
      <div className="mt-4 grid max-w-3xl gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor={`${baseId}-name`} className={FORM_LABEL}>
            Name
          </label>
          <input
            id={`${baseId}-name`}
            value={name}
            onChange={(event) => setName(event.target.value.toUpperCase())}
            placeholder="MY_PROVIDER_API_KEY"
            spellCheck={false}
            autoComplete="off"
            {...NOT_A_LOGIN_FIELD}
            autoFocus
            className={`${TEXT_FIELD} font-mono`}
          />
        </div>
        <div>
          <label htmlFor={`${baseId}-value`} className={FORM_LABEL}>
            Value
          </label>
          <input
            id={`${baseId}-value`}
            type={secret ? 'password' : 'text'}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            spellCheck={false}
            autoComplete={secret ? 'new-password' : 'off'}
            {...NOT_A_LOGIN_FIELD}
            className={TEXT_FIELD}
          />
        </div>
      </div>
      <label className="mt-3 flex items-center gap-1.5 text-[13px] text-gray-700">
        <input
          type="checkbox"
          checked={secret}
          disabled={secretByName}
          aria-describedby={secretByName ? `${baseId}-secret-note` : undefined}
          onChange={(event) => setSecretChoice(event.target.checked)}
          className={CHECKBOX}
        />
        Secret (write-only; never shown again)
      </label>
      {secretByName ? (
        <p id={`${baseId}-secret-note`} className="mt-1 text-[11px] text-gray-500">
          A name like this one is always secret.
        </p>
      ) : null}
      {error ? (
        <p className="mt-2 text-[12px] text-red-600" role="alert">
          {error}
        </p>
      ) : null}
      <div className="mt-4 flex justify-end gap-2 border-t border-gray-100 pt-4">
        <button type="button" onClick={onCancel} disabled={saving} className={BUTTON_GHOST}>
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving}
          aria-busy={saving || undefined}
          className={BUTTON_PRIMARY}
        >
          {saving ? 'Adding…' : 'Add'}
        </button>
      </div>
    </form>
  );
}
