'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import {
  ConfigEntryField,
  NOT_A_LOGIN_FIELD,
} from '@/components/features/configuration/ConfigEntryField';
import { ChangeList } from '@/components/features/admin/confirm/ChangeList';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
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
  patchConfig,
  resetConfigKey,
  testAlertNotification,
  type ConfigEntry,
  type ConfigResponse,
  type ConfigScalar,
} from '@/lib/api/config';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A category's validated changes, waiting for the administrator to confirm them. */
interface PendingSave {
  group: ConfigGroup;
  values: Record<string, ConfigScalar>;
  /** Keys the save marks secret, in the same PATCH as `values`. */
  secrets: string[];
  /** Keys whose stored row the save removes, one DELETE each after the PATCH. */
  resets: string[];
}

function shownValue(
  entry: ConfigEntry | undefined,
  value: ConfigScalar | null | undefined,
): string {
  if (value === null || value === undefined || value === '') return '(empty)';
  if (entry?.type === 'bool') return value === true ? 'On' : 'Off';
  return String(value);
}

/** One line of the confirmation: what the save does to `key`. Never a secret's value. */
function describeChange(
  entry: ConfigEntry | undefined,
  key: string,
  { values, secrets, resets }: PendingSave,
): string[] {
  const lines: string[] = [];
  if (resets.includes(key)) {
    if (entry?.custom) lines.push('Delete the variable');
    else if (entry?.secret) lines.push('Reset to the environment or default');
    else lines.push(`${shownValue(entry, entry?.value)} → environment or default`);
  }
  if (key in values) {
    const value = values[key];
    if (entry?.secret) lines.push(value === '' ? 'Clear' : 'Replace');
    else lines.push(`${shownValue(entry, entry?.value)} → ${shownValue(entry, value)}`);
  }
  if (secrets.includes(key)) lines.push('Mark as secret');
  return lines;
}

/** Keys of `record` that belong to `group`. */
function groupKeys(group: ConfigGroup, record: Readonly<Record<string, unknown>>): string[] {
  return group.entries.map((entry) => entry.key).filter((key) => key in record);
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
  // One category is edited at a time; every other one shows its saved values.
  const [editingGroup, setEditingGroup] = useState<string | null>(null);
  // Staged like drafts: nothing is written until the category's save is confirmed.
  const [pendingResets, setPendingResets] = useState<Record<string, true>>({});
  const [pendingSecrets, setPendingSecrets] = useState<Record<string, true>>({});
  const [adding, setAdding] = useState(false);
  const [alertChannelDraft, setAlertChannelDraft] = useState<AlertChannelChoice | null>(null);
  const [testingAlert, setTestingAlert] = useState(false);
  const alertTestInFlight = useRef(false);
  const [alertTestResult, setAlertTestResult] = useState<AlertTestResult | null>(null);
  const testConfirmation = useConfirmAction();
  const [pendingSave, setPendingSave] = useState<PendingSave | null>(null);
  const saveInFlight = useRef(false);

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
      setPendingResets((prev) => withoutKeys(prev, writtenKeys));
      setPendingSecrets((prev) => withoutKeys(prev, writtenKeys));
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
    // A reset staged under the old channel would delete the row the new one writes.
    setPendingResets((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    setGroupErrors((prev) => withoutKeys(prev, [webhookEntries.slack.category]));
    setAlertTestResult(null);
  };

  // A test send posts a real message, so it is confirmed like any other action.
  // Its outcome is shown beside the button, never in the dialog: only the
  // console's own sanitized message may describe a webhook failure.
  const sendAlertTest = () => {
    if (!webhookEntries || alertChannel === 'none' || alertTestInFlight.current) return;
    const entry = selectedWebhookEntry(webhookEntries, alertChannel);
    if (!entry || alertWebhookError(entry, drafts)) return;
    const draft = drafts[entry.key];
    const channel = alertChannel;
    const name = channel === 'wecom' ? 'WeCom' : 'Slack';
    testConfirmation.request({
      title: `Send a test notification to ${name}?`,
      confirmLabel: 'Send test',
      busyLabel: 'Sending…',
      body: (
        <p>
          A test message is posted to the {name} channel{' '}
          {draft?.kind === 'replace'
            ? 'behind the webhook URL you typed. That URL is not saved.'
            : 'behind the saved webhook.'}{' '}
          Settings are not changed.
        </p>
      ),
      run: async () => {
        alertTestInFlight.current = true;
        setTestingAlert(true);
        setAlertTestResult(null);
        try {
          const result = await testAlertNotification({
            channel,
            ...(draft?.kind === 'replace' ? { webhook_url: draft.value.trim() } : {}),
          });
          setAlertTestResult({ sent: true, testId: result.test_id });
        } catch (e) {
          setAlertTestResult({ sent: false, message: alertTestFailureMessage(e) });
        } finally {
          alertTestInFlight.current = false;
          setTestingAlert(false);
        }
      },
    });
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
        // The edited category stays, so its Cancel and Save stay reachable.
        .filter((group) => group.entries.length > 0 || group.id === editingGroup),
    [groups, missingOnly, query, editingGroup],
  );

  const saveGroup = (group: ConfigGroup) => {
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
    const resets = groupKeys(group, pendingResets);
    // A reset is the more specific request: the row goes, so nothing is written to it first.
    for (const key of resets) delete values[key];
    const secrets = groupKeys(group, pendingSecrets);
    if (Object.keys(values).length + secrets.length + resets.length === 0) return;
    // Nothing is written until the administrator confirms the list of changes.
    setPendingSave({ group, values, secrets, resets });
  };

  const commitSave = async ({ group, values, secrets, resets }: PendingSave) => {
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    const channelEntries = alertWebhookEntries(group.entries);
    const keys = Object.keys(values);
    setSavingGroup(group.id);
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
    let attempted = keys;
    // What was written before a failure, so the message can say so.
    let written = 0;
    let failedReset: string | null = null;
    try {
      if (keys.length > 0 || secrets.length > 0) {
        attempted = [...new Set([...keys, ...secrets])];
        const next = await patchConfig(
          secrets.length > 0
            ? { values, secrets: Object.fromEntries(secrets.map((key) => [key, true])) }
            : { values },
        );
        // Claim nothing the response does not show: a backend that ignored the
        // flag would answer with the variable unchanged.
        const unmarked = secrets.find(
          (key) => !next.entries.some((candidate) => candidate.key === key && candidate.secret),
        );
        if (unmarked) {
          // The values in the same PATCH were written; the flag was not.
          if (keys.length > 0) applyResponse(next, keys);
          throw new Error(`The backend answered, but ${unmarked} is still not secret.`);
        }
        applyResponse(next, attempted);
        written += attempted.length;
        // The channel is written now; a retry after a failed reset must not send it again.
        if (channelEntries) setAlertChannelDraft(null);
      }
      for (const key of resets) {
        attempted = [key];
        failedReset = key;
        applyResponse(await resetConfigKey(key), [key]);
        failedReset = null;
        written += 1;
      }
      if (channelEntries) {
        setAlertChannelDraft(null);
        setAlertTestResult(null);
      }
      const count = new Set([...keys, ...secrets, ...resets]).size;
      setPendingSave(null);
      setEditingGroup(null);
      toast.success(
        count === 1 ? `Saved ${group.label}.` : `Saved ${count} changes to ${group.label}.`,
      );
    } catch (e) {
      // What was not written stays as a draft, with the reason beside the save.
      const reason = getErrorMessage(e);
      const keyed = errorForKey(reason, attempted);
      if (keyed) setEntryErrors((prev) => ({ ...prev, [keyed.key]: keyed.reason }));
      let message = failedReset ? `${failedReset} could not be reset: ${reason}` : reason;
      if (written > 0) {
        message += ` ${plural(written, 'change')} ${written === 1 ? 'was' : 'were'} saved; the rest are still unsaved.`;
      }
      setGroupErrors((prev) => ({ ...prev, [group.id]: message }));
      setPendingSave(null);
    } finally {
      saveInFlight.current = false;
      setSavingGroup(null);
    }
  };

  const startEditing = (group: ConfigGroup) => {
    if (editingGroup !== null) return;
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
    setEditingGroup(group.id);
  };

  /** Cancel: every draft and staged action of the category goes, and it is read-only again. */
  const cancelEditing = (group: ConfigGroup) => {
    if (saveInFlight.current) return;
    if (alertWebhookEntries(group.entries)) {
      if (alertTestInFlight.current) return;
      setAlertChannelDraft(null);
      setAlertTestResult(null);
    }
    const keys = group.entries.map((entry) => entry.key);
    setDrafts((prev) => withoutKeys(prev, keys));
    setEntryErrors((prev) => withoutKeys(prev, keys));
    setPendingResets((prev) => withoutKeys(prev, keys));
    setPendingSecrets((prev) => withoutKeys(prev, keys));
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
    setEditingGroup(null);
  };

  const toggleReset = (target: ConfigEntry) => {
    const { key } = target;
    if (pendingResets[key]) {
      setPendingResets((prev) => withoutKeys(prev, [key]));
      return;
    }
    // The row goes, so an edit to it or a flag on it would be lost: drop them now.
    setPendingResets((prev) => ({ ...prev, [key]: true }));
    setDrafts((prev) => withoutKeys(prev, [key]));
    setEntryErrors((prev) => withoutKeys(prev, [key]));
    setPendingSecrets((prev) => withoutKeys(prev, [key]));
    if (isAlertWebhook(key)) setAlertTestResult(null);
  };

  const toggleMarkSecret = (target: ConfigEntry) => {
    const { key } = target;
    setPendingSecrets((prev) => (prev[key] ? withoutKeys(prev, [key]) : { ...prev, [key]: true }));
  };

  if (loading && !config) {
    return <div className="mt-5 py-8 text-center text-[13px] text-gray-400">Loading...</div>;
  }

  if (!config) {
    return (
      <div className="mt-5 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600" role="alert">
        {loadError ?? 'Configuration could not be loaded.'}{' '}
        <button type="button" onClick={() => void load()} className="ml-2 font-semibold underline">
          Retry
        </button>
      </div>
    );
  }

  const existingKeys = new Set(config.entries.map((entry) => entry.key));
  const resetKeys = new Set(Object.keys(pendingResets));

  return (
    <div className="mt-5 space-y-6">
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

      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor={searchId} className="sr-only">
          Search settings
        </label>
        <input
          id={searchId}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search by name, description or model"
          className="min-w-[14rem] flex-1 rounded-md border border-gray-300 px-3 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
        />
        <label className="flex items-center gap-1.5 text-[13px] text-gray-700">
          <input
            type="checkbox"
            checked={missingOnly}
            onChange={(event) => setMissingOnly(event.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
          />
          Missing only
        </label>
        <button
          type="button"
          onClick={() => setAdding(true)}
          disabled={adding}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-40"
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
        <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
          {missingOnly && config.missing.length === 0 && !query.trim()
            ? 'No required settings are missing.'
            : 'No settings match.'}
        </div>
      ) : null}

      {visibleGroups.map((visible) => {
        const group = groups.find((candidate) => candidate.id === visible.id) ?? visible;
        const editing = editingGroup === group.id;
        const channelEntries = alertWebhookEntries(group.entries);
        const selectedEntry = channelEntries
          ? selectedWebhookEntry(channelEntries, alertChannel)
          : null;
        // Each variable counts once, however many things the save does to it,
        // as the confirmation counts them; the channel choice counts as one.
        const changedKeys = new Set([
          ...group.entries
            .filter(
              (entry) =>
                (!channelEntries || !isAlertWebhook(entry.key)) &&
                isDirty(entry, drafts[entry.key]),
            )
            .map((entry) => entry.key),
          ...groupKeys(group, pendingResets),
          ...groupKeys(group, pendingSecrets),
        ]);
        const channelDirty =
          channelEntries !== null &&
          (alertChannelDraft !== null ||
            Boolean(selectedEntry && isDirty(selectedEntry, drafts[selectedEntry.key])));
        const dirty = changedKeys.size + Number(channelDirty);
        const hidden = group.entries.length - visible.entries.length;
        const saving = savingGroup === group.id;
        const busy = saving || Boolean(channelEntries && testingAlert);
        const headingId = `${searchId}-${group.id}`;
        return (
          <section
            key={group.id}
            aria-labelledby={headingId}
            className={`rounded-xl border bg-white p-5 ${editing ? 'border-gray-400 shadow-sm' : 'border-gray-200'}`}
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 id={headingId} className="text-[14px] font-semibold text-gray-900">
                    {group.label}
                  </h2>
                  {editing ? (
                    <span className="rounded bg-gray-900 px-1.5 py-0.5 text-[10px] font-medium text-white">
                      Editing
                    </span>
                  ) : null}
                </div>
                {group.description ? (
                  <p className="mt-1 text-[12px] text-gray-500">{group.description}</p>
                ) : null}
              </div>
              {editing ? null : (
                <button
                  type="button"
                  onClick={() => startEditing(group)}
                  disabled={editingGroup !== null}
                  title={
                    editingGroup !== null
                      ? 'Save or cancel the category you are editing first.'
                      : undefined
                  }
                  aria-label={`Edit ${group.label}`}
                  className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-40"
                >
                  Edit
                </button>
              )}
            </div>

            {/* A form, so Enter saves the category, and one browsers do not
                offer to remember: a secret typed here is not a login. The
                console checks the values itself, so the browser does not. */}
            <form
              autoComplete="off"
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                if (editing && !busy && dirty > 0) saveGroup(group);
              }}
            >
              {editing && hidden > 0 ? (
                <p className="mt-3 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-600">
                  {hidden === group.entries.length
                    ? 'Every setting in this category is hidden by the current filter.'
                    : `${plural(hidden, 'setting')} in this category ${hidden === 1 ? 'is' : 'are'} hidden by the current filter.`}{' '}
                  Unsaved changes to them are kept and saved with the rest.
                </p>
              ) : null}
              <div className="mt-1 divide-y divide-gray-100">
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
                    onTest={sendAlertTest}
                    onReset={toggleReset}
                    readOnly={!editing}
                    pendingResets={resetKeys}
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
                      onReset={toggleReset}
                      onMarkSecret={toggleMarkSecret}
                      readOnly={!editing}
                      resetPending={Boolean(pendingResets[entry.key])}
                      markSecretPending={Boolean(pendingSecrets[entry.key])}
                      disabled={busy}
                    />
                  ))}
              </div>

              {editing ? (
                // Sticky, so Cancel and Save stay in reach anywhere in a long category.
                <div className="sticky bottom-0 z-10 -mx-5 -mb-5 mt-2 rounded-b-xl border-t border-gray-200 bg-white/95 px-5 py-3 backdrop-blur">
                  {groupErrors[group.id] ? (
                    <p
                      className="mb-2 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600"
                      role="alert"
                    >
                      {groupErrors[group.id]}
                    </p>
                  ) : null}
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    <span
                      className={`mr-auto text-[12px] ${dirty > 0 ? 'font-medium text-amber-700' : 'text-gray-500'}`}
                      role="status"
                    >
                      {dirty > 0 ? `${plural(dirty, 'unsaved change')}` : 'No changes yet'}
                    </span>
                    <button
                      type="button"
                      onClick={() => cancelEditing(group)}
                      disabled={busy}
                      aria-label={`Cancel editing ${group.label}`}
                      className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-40"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={busy || dirty === 0}
                      aria-busy={saving || undefined}
                      aria-label={`Save changes to ${group.label}`}
                      className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
                    >
                      {saving ? 'Saving…' : 'Save changes'}
                    </button>
                  </div>
                </div>
              ) : null}
            </form>
          </section>
        );
      })}

      {testConfirmation.dialog}

      {pendingSave ? (
        <ConfirmDialog
          title={`Save ${plural(
            new Set([
              ...Object.keys(pendingSave.values),
              ...pendingSave.secrets,
              ...pendingSave.resets,
            ]).size,
            'change',
          )} to ${pendingSave.group.label}?`}
          confirmLabel="Confirm save"
          cancelLabel="Back to editing"
          busyLabel="Saving…"
          busy={savingGroup === pendingSave.group.id}
          onConfirm={() => void commitSave(pendingSave)}
          onCancel={() => setPendingSave(null)}
        >
          <ul className="space-y-1.5">
            {pendingSave.group.entries
              .filter(
                (entry) =>
                  entry.key in pendingSave.values ||
                  pendingSave.secrets.includes(entry.key) ||
                  pendingSave.resets.includes(entry.key),
              )
              .map((entry) => (
                <li key={entry.key} className="break-all">
                  <span className="font-mono font-medium text-gray-900">{entry.key}</span>
                  {describeChange(entry, entry.key, pendingSave).map((line) => (
                    <span key={line} className="block font-mono text-[12px]">
                      {line}
                    </span>
                  ))}
                </li>
              ))}
          </ul>
          {pendingSave.resets.length > 0 ? (
            <p>
              A reset removes the stored value: the setting falls back to the environment, then to
              its default. A custom variable is deleted.
            </p>
          ) : null}
          {pendingSave.secrets.length > 0 ? (
            <p className="font-medium text-gray-800">
              Marking a variable secret cannot be undone from the console.
            </p>
          ) : null}
          {[...Object.keys(pendingSave.values), ...pendingSave.secrets, ...pendingSave.resets].some(
            (key) =>
              pendingSave.group.entries.find((candidate) => candidate.key === key)
                ?.restart_required,
          ) ? (
            <p>Some of these apply only after the backend restarts.</p>
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
  const confirmation = useConfirmAction();
  const saving = confirmation.busy;
  const secretByName = looksSecret(name);
  const secret = secretByName || secretChoice;

  const submit = (event: React.FormEvent) => {
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
    setError(null);
    // A new variable is reviewed before it is written; a secret's value is never shown.
    confirmation.request({
      title: `Add ${key}?`,
      confirmLabel: 'Add variable',
      busyLabel: 'Adding…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={[
              {
                label: key,
                after: secret ? (value ? 'Set (hidden)' : '(empty)') : value || '(empty)',
              },
              { label: 'Secret', after: secret ? 'Yes, write-only from now on' : 'No' },
            ]}
          />
          <p>
            It is stored in the database, where a{' '}
            <code className="rounded bg-gray-100 px-1">{`$\{${key}}`}</code> reference in a
            configuration file can read it.
          </p>
        </>
      ),
      run: async () => {
        const next = await patchConfig({ values: { [key]: value }, secrets: { [key]: secret } });
        onAdded(next, key);
      },
    });
  };

  return (
    // `autoComplete="off"` and the value field's attributes: a name and a
    // secret are not a login, and the browser must not offer to save them.
    <form
      onSubmit={(event) => void submit(event)}
      autoComplete="off"
      aria-labelledby={`${baseId}-title`}
      className="rounded-xl border border-gray-200 bg-white p-5"
    >
      <h2 id={`${baseId}-title`} className="text-[14px] font-semibold text-gray-900">
        Add variable
      </h2>
      <p className="mt-1 text-[12px] text-gray-500">
        For a <code className="rounded bg-gray-100 px-1">{'${VAR}'}</code> reference in a
        configuration file that is not listed here.
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor={`${baseId}-name`} className="text-[12px] font-medium text-gray-700">
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
            className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 font-mono text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor={`${baseId}-value`} className="text-[12px] font-medium text-gray-700">
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
            className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
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
          className="h-4 w-4 rounded border-gray-300 disabled:opacity-60"
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
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving}
          aria-busy={saving || undefined}
          className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
        >
          {saving ? 'Adding…' : 'Review and add'}
        </button>
      </div>
      {confirmation.dialog}
    </form>
  );
}
