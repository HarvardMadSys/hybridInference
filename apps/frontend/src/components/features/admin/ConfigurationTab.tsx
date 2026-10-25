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
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSectionNav,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  CONTROL,
  FORM_LABEL,
  INDEXED_LAYOUT,
  NOTICE_ACTION,
  PANEL,
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
      } else if (!failedReset && resets.length > 0) {
        // The backend judged the value changes without the resets, which were
        // never sent, so its "in the same save" can read as if they had been.
        message = `${/[.!?]$/.test(message) ? message : `${message}.`} ${resets.join(', ')} ${
          resets.length === 1 ? 'was' : 'were'
        } not reset: resets are sent after the value changes, one request each. If this change needs a reset, save the reset on its own first.`;
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
  const resetKeys = new Set(Object.keys(pendingResets));

  /**
   * Unsaved changes in a whole category, including entries a filter hides.
   * Each variable counts once, however many things the save does to it, as the
   * confirmation counts them; the alert channel choice counts as one.
   */
  const dirtyCount = (group: ConfigGroup) => {
    const channelEntries = alertWebhookEntries(group.entries);
    const selectedEntry = channelEntries
      ? selectedWebhookEntry(channelEntries, alertChannel)
      : null;
    const changedKeys = new Set([
      ...group.entries
        .filter(
          (entry) =>
            (!channelEntries || !isAlertWebhook(entry.key)) && isDirty(entry, drafts[entry.key]),
        )
        .map((entry) => entry.key),
      ...groupKeys(group, pendingResets),
      ...groupKeys(group, pendingSecrets),
    ]);
    const channelDirty =
      channelEntries !== null &&
      (alertChannelDraft !== null ||
        Boolean(selectedEntry && isDirty(selectedEntry, drafts[selectedEntry.key])));
    return changedKeys.size + Number(channelDirty);
  };
  const fullGroup = (visible: ConfigGroup) =>
    groups.find((candidate) => candidate.id === visible.id) ?? visible;
  const sectionAnchor = (group: ConfigGroup) => `config-${group.id.replace(/[^\w-]/g, '-')}`;

  // The index lists the categories the filters leave, and flags those with a
  // missing required setting or unsaved changes.
  const navItems = visibleGroups.map((visible) => {
    const group = fullGroup(visible);
    const missing = group.entries.filter((entry) => entry.missing).length;
    const dirty = dirtyCount(group);
    return {
      id: sectionAnchor(group),
      label: group.label,
      meta:
        missing > 0 || dirty > 0 ? (
          <span className="flex shrink-0 items-center gap-1.5">
            {dirty > 0 ? (
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden="true" />
            ) : null}
            {dirty > 0 ? <span className="sr-only">, unsaved changes</span> : null}
            {missing > 0 ? (
              <span className="rounded bg-red-50 px-1 text-[11px] font-medium tabular-nums text-red-700">
                {missing}
                <span className="sr-only"> missing</span>
              </span>
            ) : null}
          </span>
        ) : undefined,
    };
  });

  return (
    <div className="mt-6 space-y-4">
      {config.pending_restart.length > 0 ? (
        <section
          aria-label="Restart pending"
          className="rounded-lg border border-amber-200 bg-amber-50 p-4"
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
          className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[12px] text-red-800"
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

      <div className="flex flex-wrap items-center gap-2">
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

      {navItems.length > 0 ? (
        // A category index beside full-width categories, one after another.
        <div className={INDEXED_LAYOUT}>
          <AdminSectionNav label="Configuration categories" items={navItems} />
          <div className="min-w-0 space-y-8">
            {visibleGroups.map((visible) => {
              const group = fullGroup(visible);
              const editing = editingGroup === group.id;
              const channelEntries = alertWebhookEntries(group.entries);
              const dirty = dirtyCount(group);
              const hidden = group.entries.length - visible.entries.length;
              const saving = savingGroup === group.id;
              const busy = saving || Boolean(channelEntries && testingAlert);
              const headingId = `${searchId}-${group.id}`;
              return (
                <section
                  key={group.id}
                  id={sectionAnchor(group)}
                  aria-labelledby={headingId}
                  className="scroll-mt-4 focus:outline-none"
                >
                  <div className="mb-3 flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
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
                        <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">
                          {group.description}
                        </p>
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
                        className={BUTTON_SECONDARY}
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
                      <p className="mb-3 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-600">
                        {hidden === group.entries.length
                          ? 'Every setting in this category is hidden by the current filter.'
                          : `${plural(hidden, 'setting')} in this category ${hidden === 1 ? 'is' : 'are'} hidden by the current filter.`}{' '}
                        Unsaved changes to them are kept and saved with the rest.
                      </p>
                    ) : null}
                    <div className={`${PANEL} ${editing ? 'border-gray-400 shadow-sm' : ''}`}>
                      <div className="divide-y divide-gray-100 px-4">
                        {channelEntries &&
                        visible.entries.some((entry) => isAlertWebhook(entry.key)) ? (
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
                              layout="split"
                              readOnly={!editing}
                              resetPending={Boolean(pendingResets[entry.key])}
                              markSecretPending={Boolean(pendingSecrets[entry.key])}
                              disabled={busy}
                            />
                          ))}
                      </div>

                      {editing ? (
                        // Sticky, so Cancel and Save stay in reach anywhere in a long category.
                        <div className="sticky bottom-0 z-10 rounded-b-lg border-t border-gray-200 bg-white/95 px-4 py-3 backdrop-blur">
                          {groupErrors[group.id] ? (
                            <div className="mb-2">
                              <AdminNotice role="alert">{groupErrors[group.id]}</AdminNotice>
                            </div>
                          ) : null}
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            <span
                              className={`mr-auto text-[12px] ${dirty > 0 ? 'font-medium text-amber-700' : 'text-gray-500'}`}
                              role="status"
                            >
                              {dirty > 0 ? plural(dirty, 'unsaved change') : 'No changes yet'}
                            </span>
                            <button
                              type="button"
                              onClick={() => cancelEditing(group)}
                              disabled={busy}
                              aria-label={`Cancel editing ${group.label}`}
                              className={BUTTON_GHOST}
                            >
                              Cancel
                            </button>
                            <button
                              type="submit"
                              disabled={busy || dirty === 0}
                              aria-busy={saving || undefined}
                              aria-label={`Save changes to ${group.label}`}
                              className={BUTTON_PRIMARY}
                            >
                              {saving ? 'Saving…' : 'Save changes'}
                            </button>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  </form>
                </section>
              );
            })}
          </div>
        </div>
      ) : null}

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
          {/* Each request is validated alone (PATCH, then one DELETE per reset). */}
          {pendingSave.resets.length > 0 &&
          (pendingSave.resets.length > 1 ||
            Object.keys(pendingSave.values).length + pendingSave.secrets.length > 0) ? (
            <p>
              {Object.keys(pendingSave.values).length + pendingSave.secrets.length > 0
                ? 'The value changes are sent first, in one request; then each reset, on its own.'
                : 'Each reset is sent on its own, in the order listed.'}{' '}
              The backend checks each request by itself, so a change that is valid only together
              with one of these resets can be refused; if so, save that reset on its own first.
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
      className={`${PANEL} p-4`}
    >
      <h2 id={`${baseId}-title`} className="text-[14px] font-semibold text-gray-900">
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
          {saving ? 'Adding…' : 'Review and add'}
        </button>
      </div>
      {confirmation.dialog}
    </form>
  );
}
