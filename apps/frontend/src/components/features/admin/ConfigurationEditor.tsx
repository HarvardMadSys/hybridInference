'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import {
  ConfigEntryField,
  NOT_A_LOGIN_FIELD,
} from '@/components/features/configuration/ConfigEntryField';
import { ChangeList } from '@/components/features/admin/confirm/ChangeList';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { withoutSecret } from '@/components/features/admin/confirm/withoutSecret';
import {
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  CONTROL,
  FIELD_LABEL,
  FORM_LABEL,
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
import { LiveConfigStatus } from '@/components/features/configuration/LiveConfigStatus';
import {
  chainVersions,
  noteEdit,
  saveBase,
  withoutVersions,
  type DraftVersions,
} from '@/components/features/configuration/draftVersions';
import { ADMIN_SETTINGS_PATH } from '@/components/features/configuration/paths';
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
  exportConfigBackup,
  getConfig,
  patchConfig,
  resetConfigKey,
  restoreConfigBackup,
  testAlertNotification,
  type ConfigBackupPayload,
  type ConfigEntry,
  type ConfigResponse,
  type ConfigScalar,
  type ConfigSourceVersion,
} from '@/lib/api/config';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function downloadBackupJson(payload: ConfigBackupPayload): void {
  const datePart = (payload.exported_at ?? new Date().toISOString()).slice(0, 10);
  const filename = `hybrid-inference-config-backup-${datePart}.json`;
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
    type: 'application/json',
  });
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  if (typeof URL.revokeObjectURL === 'function') {
    URL.revokeObjectURL(url);
  }
}

/** A category's validated changes, as its save sends them. */
interface CategorySave {
  group: ConfigGroup;
  values: Record<string, ConfigScalar>;
  /** Keys the save marks secret, in the same PATCH as `values`. */
  secrets: string[];
  /** Keys whose stored row the save removes, one DELETE each after the PATCH. */
  resets: string[];
  /**
   * The version the category's changes were made on, pinned when the save
   * starts: a read that completes while it is in flight cannot move it.
   */
  expectedVersion: ConfigSourceVersion | null;
}

/**
 * The `If-Match` for a write: the version the category's changes were made on,
 * or, within one save, the previous request's own `saved_version`. An older
 * backend reports none, and its writes stay unconditional.
 */
function versioned(
  version: ConfigSourceVersion | null | undefined,
): [] | [{ expectedVersion: ConfigSourceVersion }] {
  return version ? [{ expectedVersion: version }] : [];
}

/**
 * The version to chain the next request of the same save on: the one this
 * write committed. Never the response's `source_version`, which can include a
 * save made elsewhere in the meantime. A write that stored nothing commits no
 * version, and the chain stays where it was.
 */
function committedVersion(
  next: ConfigResponse,
  base: ConfigSourceVersion | null,
): ConfigSourceVersion | null {
  return next.saved_version ?? base;
}

/** Whether a write was refused because the configuration changed since it was read. */
function isVersionConflict(error: unknown): boolean {
  return error instanceof APIError && error.code === 'CONFIG_VERSION_CONFLICT';
}

/**
 * The Add variable form's entry in `draftVersions`: the version it opened on,
 * moved like a category's by this page's own writes and by a reload after a
 * conflict. Not a category id.
 */
const ADD_VARIABLE_DRAFT = '\u0000add-variable';

/** The live configuration groups a category's settings belong to, once each. */
function liveGroupNames(group: ConfigGroup): string[] {
  return [...new Set(group.entries.flatMap((entry) => (entry.group ? [entry.group] : [])))];
}

/** Keys of `record` that belong to `group`. */
function groupKeys(group: ConfigGroup, record: Readonly<Record<string, unknown>>): string[] {
  return group.entries.map((entry) => entry.key).filter((key) => key in record);
}

/**
 * The configuration on Admin → Settings: every database-backed setting,
 * grouped by category, with the categories spread over the page's sub-tabs.
 *
 * One editor holds every category's state, so a change waits unsaved while
 * the administrator looks at another sub-tab. Settings are edited in place,
 * and several categories can hold unsaved changes at once. Each category's
 * Save writes its own changes as one batch (the backend validates a batch
 * together, so related settings can change in one save). After any write the
 * page asks the server components to re-render, which re-reads the public site
 * configuration and so updates the "required settings are missing" banner.
 *
 * The components below draw the editor's parts: its notices, its filters, a
 * category, the Add variable form and the backup.
 */
export function useConfigurationEditor({
  initialMissingOnly = false,
}: { initialMissingOnly?: boolean } = {}) {
  const toast = useAccountToast();
  const router = useRouter();
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // Whether the first load has answered, successfully or not: until then the
  // page has nothing to place in its sub-tabs.
  const [settled, setSettled] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<ConfigDrafts>({});
  const [entryErrors, setEntryErrors] = useState<Record<string, string>>({});
  const [groupErrors, setGroupErrors] = useState<Record<string, string>>({});
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [missingOnly, setMissingOnly] = useState(initialMissingOnly);
  // Staged like drafts: nothing is written until the category is saved.
  const [pendingResets, setPendingResets] = useState<Record<string, true>>({});
  const [pendingSecrets, setPendingSecrets] = useState<Record<string, true>>({});
  const [alertChannelDraft, setAlertChannelDraft] = useState<AlertChannelChoice | null>(null);
  const [testingAlert, setTestingAlert] = useState(false);
  const alertTestInFlight = useRef(false);
  const [alertTestResult, setAlertTestResult] = useState<AlertTestResult | null>(null);
  // Restoring a backup overwrites everything, so it is confirmed; a download is not.
  const backupConfirmation = useConfirmAction();
  const saveInFlight = useRef(false);
  // The version each category's unsaved changes were made on (draftVersions).
  const [draftVersions, setDraftVersions] = useState<DraftVersions>({});
  // The version on screen when the administrator acts: set once each render
  // is committed, so an event handler reads the version its page showed.
  const shownVersion = useRef<ConfigSourceVersion | null>(null);
  useEffect(() => {
    shownVersion.current = config?.source_version ?? null;
  }, [config]);
  const categoryOf = useRef<Map<string, string>>(new Map());
  useEffect(() => {
    categoryOf.current = new Map(
      (config?.entries ?? []).map((entry) => [entry.key, entry.category]),
    );
  }, [config]);
  /** A change is staged in the categories of `keys`: they keep the version shown now. */
  const noteEdits = useCallback((keys: readonly string[]) => {
    setDraftVersions((prev) =>
      keys.reduce((versions, key) => {
        const groupId = categoryOf.current.get(key);
        return groupId === undefined ? versions : noteEdit(versions, groupId, shownVersion.current);
      }, prev),
    );
  }, []);

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
      setSettled(true);
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

  const onDraftChange = useCallback(
    (key: string, draft: ConfigDraft | undefined) => {
      if (draft) noteEdits([key]);
      setDrafts((prev) => {
        const next = { ...prev };
        if (draft) next[key] = draft;
        else delete next[key];
        return next;
      });
      setEntryErrors((prev) => withoutKeys(prev, [key]));
      if (isAlertWebhook(key)) setAlertTestResult(null);
    },
    [noteEdits],
  );

  const webhookEntries = config ? alertWebhookEntries(config.entries) : null;
  const alertChannel =
    alertChannelDraft ?? (webhookEntries ? savedAlertChannel(webhookEntries) : 'none');

  const changeAlertChannel = (channel: AlertChannelChoice) => {
    if (!webhookEntries || alertTestInFlight.current) return;
    noteEdits(ALERT_WEBHOOK_KEYS);
    setAlertChannelDraft(channel === savedAlertChannel(webhookEntries) ? null : channel);
    setDrafts((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    setEntryErrors((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    // A reset staged under the old channel would delete the row the new one writes.
    setPendingResets((prev) => withoutKeys(prev, ALERT_WEBHOOK_KEYS));
    setGroupErrors((prev) => withoutKeys(prev, [webhookEntries.slack.category]));
    setAlertTestResult(null);
  };

  // A test send posts a message but changes no setting, so it goes on the
  // click. Its outcome is shown beside the button: only the console's own
  // sanitized message may describe a webhook failure.
  const sendAlertTest = async () => {
    if (!webhookEntries || alertChannel === 'none' || alertTestInFlight.current) return;
    const entry = selectedWebhookEntry(webhookEntries, alertChannel);
    if (!entry || alertWebhookError(entry, drafts)) return;
    const draft = drafts[entry.key];
    const channel = alertChannel;
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
  };

  const groups = useMemo(
    () => (config ? groupEntries(config.categories, config.entries) : []),
    [config],
  );

  /**
   * Unsaved changes in a whole category, including entries a filter hides.
   * Each variable counts once, however many things the save does to it; the
   * alert channel choice counts as one.
   */
  const dirtyCount = useCallback(
    (group: ConfigGroup) => {
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
    },
    [drafts, pendingResets, pendingSecrets, alertChannel, alertChannelDraft],
  );

  const filtering = missingOnly || query.trim() !== '';

  const visibleGroups = useMemo(
    () =>
      groups.flatMap((group) => {
        const entries = group.entries.filter(
          (entry) =>
            (!missingOnly || entry.missing) &&
            (matchesQuery(entry, query, group.label) ||
              (isAlertWebhook(entry.key) &&
                'notification channel'.includes(query.trim().toLowerCase()))),
        );
        // A category with unsaved changes stays, so its Discard and Save stay reachable.
        return entries.length > 0 || dirtyCount(group) > 0 ? [{ ...group, entries }] : [];
      }),
    [groups, missingOnly, query, dirtyCount],
  );

  const saveGroup = (group: ConfigGroup) => {
    // One save at a time: a Save while another category's is in flight does nothing.
    if (saveInFlight.current) return;
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
    void commitSave({
      group,
      values,
      secrets,
      resets,
      // Taken now, from the version this category's changes were made on: a read
      // that completes while the save is in flight cannot move it.
      expectedVersion: saveBase(draftVersions, group.id, config?.source_version),
    });
  };

  const commitSave = async ({ group, values, secrets, resets, expectedVersion }: CategorySave) => {
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    const channelEntries = alertWebhookEntries(group.entries);
    const keys = Object.keys(values);
    // A secret typed into this save, should a refusal echo it back. Longest
    // first, so one that contains another is masked whole.
    const typedSecrets = keys
      .flatMap((key) => {
        const value = values[key];
        const secret =
          secrets.includes(key) || group.entries.some((entry) => entry.key === key && entry.secret);
        return secret && typeof value === 'string' && value ? [value] : [];
      })
      .sort((a, b) => b.length - a.length);
    setSavingGroup(group.id);
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
    let attempted = keys;
    // What was written before a failure, so the message can say so.
    let written = 0;
    // The version this category's changes were made on. Each request after the
    // first is conditional on the one before it committed, never on a version
    // some other read brought in: anything saved in between is a conflict.
    let base = expectedVersion;
    /** Move every draft made on `from` along to what this page's own write committed. */
    const chain = (from: ConfigSourceVersion | null, to: ConfigSourceVersion | null) => {
      if (from) setDraftVersions((prev) => chainVersions(prev, from, to));
    };
    let failedReset: string | null = null;
    try {
      if (keys.length > 0 || secrets.length > 0) {
        attempted = [...new Set([...keys, ...secrets])];
        const next = await patchConfig(
          secrets.length > 0
            ? { values, secrets: Object.fromEntries(secrets.map((key) => [key, true])) }
            : { values },
          ...versioned(base),
        );
        const committed = committedVersion(next, base);
        chain(base, committed);
        base = committed;
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
        const next = await resetConfigKey(key, ...versioned(base));
        const committed = committedVersion(next, base);
        chain(base, committed);
        base = committed;
        applyResponse(next, [key]);
        failedReset = null;
        written += 1;
      }
      if (channelEntries) {
        setAlertChannelDraft(null);
        setAlertTestResult(null);
      }
      const count = new Set([...keys, ...secrets, ...resets]).size;
      setDraftVersions((prev) => withoutVersions(prev, [group.id]));
      toast.success(
        count === 1 ? `Saved ${group.label}.` : `Saved ${count} changes to ${group.label}.`,
      );
    } catch (e) {
      if (isVersionConflict(e)) {
        // Saved elsewhere since this page read it: nothing of this request was
        // written. Show the latest values and keep the drafts for a second look.
        let message =
          'The configuration was changed elsewhere since this page loaded, so this was not saved. The latest values are shown now; your unsaved changes are kept. Review them and save again.';
        if (written > 0) {
          message += ` ${plural(written, 'change')} ${written === 1 ? 'was' : 'were'} saved before that.`;
        }
        setGroupErrors((prev) => ({ ...prev, [group.id]: message }));
        // The one place a draft moves to a version this page did not write:
        // the administrator has been told, and is shown the newer values.
        void getConfig()
          .then((fresh) => {
            setConfig(fresh);
            const shown = fresh.source_version;
            setDraftVersions((prev) =>
              shown ? { ...prev, [group.id]: shown } : withoutVersions(prev, [group.id]),
            );
          })
          .catch(() => undefined);
        return;
      }
      // What was not written stays as a draft, with the reason beside the save.
      const reason = typedSecrets.reduce(
        (message, secret) => withoutSecret(message, secret),
        getErrorMessage(e),
      );
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
    } finally {
      saveInFlight.current = false;
      setSavingGroup(null);
    }
  };

  /** Discard: every draft and staged action of the category goes, and its saved values are back. */
  const discardGroup = (group: ConfigGroup) => {
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
    setDraftVersions((prev) => withoutVersions(prev, [group.id]));
  };

  const toggleReset = (target: ConfigEntry) => {
    const { key } = target;
    if (pendingResets[key]) {
      setPendingResets((prev) => withoutKeys(prev, [key]));
      return;
    }
    // The row goes, so an edit to it or a flag on it would be lost: drop them now.
    noteEdits([key]);
    setPendingResets((prev) => ({ ...prev, [key]: true }));
    setDrafts((prev) => withoutKeys(prev, [key]));
    setEntryErrors((prev) => withoutKeys(prev, [key]));
    setPendingSecrets((prev) => withoutKeys(prev, [key]));
    if (isAlertWebhook(key)) setAlertTestResult(null);
  };

  // Bumped as the Add variable form opens and closes: a reload it asked for
  // that completes after it closed moves nothing. The form's open state is
  // AddVariableControl's; its version is kept here, with the categories'.
  const addSession = useRef(0);

  /** The Add variable form opens: it adds on the version shown now. */
  const openAddForm = () => {
    addSession.current += 1;
    const shown = shownVersion.current;
    setDraftVersions((prev) => {
      const rest = withoutVersions(prev, [ADD_VARIABLE_DRAFT]);
      return shown ? { ...rest, [ADD_VARIABLE_DRAFT]: shown } : rest;
    });
  };

  const closeAddForm = () => {
    addSession.current += 1;
    setDraftVersions((prev) => withoutVersions(prev, [ADD_VARIABLE_DRAFT]));
  };

  /** The form's add was written, conditional on `base`: what else was made on it moves along. */
  const variableAdded = (next: ConfigResponse, key: string, base: ConfigSourceVersion | null) => {
    if (base) setDraftVersions((prev) => chainVersions(prev, base, next.saved_version));
    applyResponse(next, [key]);
    closeAddForm();
  };

  /**
   * After the form's add met a conflict: show the latest configuration, and
   * move the form onto it, for the administrator to review and add again.
   * `null`, and the form's version left where it was, when the read fails.
   */
  const reloadForAdd = async (): Promise<ConfigResponse | null> => {
    const session = addSession.current;
    let fresh: ConfigResponse;
    try {
      fresh = await getConfig();
    } catch {
      return null;
    }
    if (session !== addSession.current) return null;
    setConfig(fresh);
    const shown = fresh.source_version;
    setDraftVersions((prev) =>
      shown
        ? { ...prev, [ADD_VARIABLE_DRAFT]: shown }
        : withoutVersions(prev, [ADD_VARIABLE_DRAFT]),
    );
    return fresh;
  };

  const toggleMarkSecret = (target: ConfigEntry) => {
    const { key } = target;
    if (!pendingSecrets[key]) noteEdits([key]);
    setPendingSecrets((prev) => (prev[key] ? withoutKeys(prev, [key]) : { ...prev, [key]: true }));
  };

  // A download changes nothing, so it goes on the click. The file holds every
  // secret in plain text: the button's title says so before, the toast after.
  const downloadBackup = async () => {
    const failure = await backupConfirmation.perform({
      run: async () => {
        downloadBackupJson(await exportConfigBackup());
        toast.success(
          'Downloaded configuration backup. It holds API keys and secrets in plain text: store it securely.',
        );
      },
    });
    if (failure) toast.error(`The backup could not be downloaded: ${failure}`);
  };

  const restoreFromFile = async (file: File) => {
    let parsed: ConfigBackupPayload;
    try {
      const text = await file.text();
      const raw: unknown = JSON.parse(text);
      if (
        !raw ||
        typeof raw !== 'object' ||
        Array.isArray(raw) ||
        typeof (raw as { schema_version?: unknown }).schema_version !== 'number'
      ) {
        toast.error('The selected file is not a valid configuration backup JSON.');
        return;
      }
      parsed = raw as ConfigBackupPayload;
    } catch {
      toast.error('The selected file is not a valid configuration backup JSON.');
      return;
    }

    const appConfigCount = Array.isArray(parsed.app_config) ? parsed.app_config.length : 0;
    const siteSettingsCount = Array.isArray(parsed.site_settings) ? parsed.site_settings.length : 0;
    const providerCount = Array.isArray(parsed.provider_definitions)
      ? parsed.provider_definitions.length
      : 0;
    const keyCount = Array.isArray(parsed.provider_keys) ? parsed.provider_keys.length : 0;
    const routeCount =
      (Array.isArray(parsed.provider_route_candidates)
        ? parsed.provider_route_candidates.length
        : 0) +
      (Array.isArray(parsed.provider_route_configs) ? parsed.provider_route_configs.length : 0);

    backupConfirmation.request({
      title: 'Restore configuration from backup?',
      confirmLabel: 'Restore configuration',
      busyLabel: 'Restoring…',
      tone: 'danger',
      body: (
        <div className="space-y-2">
          <ChangeList
            changes={[
              { label: 'Backup file', after: file.name },
              ...(parsed.exported_at ? [{ label: 'Exported at', after: parsed.exported_at }] : []),
              { label: 'Application settings', after: plural(appConfigCount, 'stored row') },
              { label: 'Runtime & model settings', after: plural(siteSettingsCount, 'setting') },
              {
                label: 'Custom providers & keys',
                after: `${plural(providerCount, 'provider')}, ${plural(keyCount, 'API key')}`,
              },
              { label: 'Routes & overrides', after: plural(routeCount, 'route') },
            ]}
          />
          <p className="font-medium text-red-800">
            Restoring replaces the current configuration, providers, API keys, routes, and runtime
            settings with the contents of this backup. Existing user API key encryption secrets are
            automatically preserved when user API keys already exist.
          </p>
        </div>
      ),
      run: async () => {
        const res = await restoreConfigBackup(parsed);
        setConfig(res.config);
        setDrafts({});
        setDraftVersions({});
        setEntryErrors({});
        setGroupErrors({});
        setPendingResets({});
        setPendingSecrets({});
        setAlertChannelDraft(null);
        setAlertTestResult(null);
        router.refresh();
        if (res.immutable_keys_preserved.length > 0) {
          toast.success(
            `Restored configuration (preserved existing ${res.immutable_keys_preserved.join(', ')}).`,
          );
        } else {
          toast.success('Restored configuration from backup.');
        }
      },
    });
  };

  return {
    config,
    loading,
    settled,
    loadError,
    load,
    query,
    setQuery,
    missingOnly,
    setMissingOnly,
    /** Whether a search or Missing only narrows the categories. */
    filtering,
    groups,
    /** The categories the filters leave, each with the entries they leave. */
    visibleGroups,
    dirtyCount,
    drafts,
    entryErrors,
    groupErrors,
    savingGroup,
    pendingResets,
    pendingSecrets,
    alertChannel,
    testingAlert,
    alertTestResult,
    onDraftChange,
    changeAlertChannel,
    sendAlertTest,
    saveGroup,
    discardGroup,
    toggleReset,
    toggleMarkSecret,
    applyResponse,
    /**
     * The version the Add variable form's add is conditional on: the one it
     * opened on, moved only by this page's own writes and by `reloadForAdd`.
     */
    addVariableVersion: saveBase(draftVersions, ADD_VARIABLE_DRAFT, config?.source_version),
    openAddForm,
    closeAddForm,
    reloadForAdd,
    variableAdded,
    downloadBackup,
    restoreFromFile,
    /** Render its `dialog` once, where no sub-tab can hide it. */
    backupConfirmation,
  };
}

export type ConfigurationEditor = ReturnType<typeof useConfigurationEditor>;

/** How many settings the filters leave, across every category. */
export function matchingSettingCount(editor: ConfigurationEditor): number {
  return editor.visibleGroups.reduce((total, group) => total + group.entries.length, 0);
}

/**
 * What needs attention whatever sub-tab is open: a configuration that could
 * not be loaded, a restart a saved setting waits for, and required settings
 * that have no value.
 */
export function ConfigurationNotices({ editor }: { editor: ConfigurationEditor }) {
  const { config, missingOnly, setMissingOnly } = editor;

  if (!config) {
    if (editor.loading) return <AdminLoading size="sm" label="Loading configuration..." />;
    return (
      <AdminNotice
        role="alert"
        action={
          <button type="button" onClick={() => void editor.load()} className={NOTICE_ACTION}>
            Retry
          </button>
        }
      >
        {editor.loadError ?? 'Configuration could not be loaded.'}
      </AdminNotice>
    );
  }

  return (
    <>
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
              <RestartBackendButton onRestarted={() => navigateTo(ADMIN_SETTINGS_PATH)} />
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
    </>
  );
}

/** The search and the Missing only filter. They look through every category, whatever its sub-tab. */
export function ConfigurationFilters({ editor }: { editor: ConfigurationEditor }) {
  const searchId = useId();
  return (
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
          value={editor.query}
          onChange={(event) => editor.setQuery(event.target.value)}
          placeholder="Search by name, description or model"
          className={`${CONTROL} w-full pl-8`}
        />
      </div>
      <label className="flex h-8 items-center gap-1.5 px-1 text-[13px] text-gray-700">
        <input
          type="checkbox"
          checked={editor.missingOnly}
          onChange={(event) => editor.setMissingOnly(event.target.checked)}
          className={CHECKBOX}
        />
        Missing only
      </label>
    </div>
  );
}

/** The anchor of a category's section. */
export function configSectionId(categoryId: string): string {
  return `config-${categoryId.replace(/[^\w-]/g, '-')}`;
}

/**
 * One configuration category, edited in place, with its own Save. Nothing
 * when the backend has no such category or the filters leave nothing of it.
 */
export function ConfigCategorySection({
  editor,
  categoryId,
  tag,
}: {
  editor: ConfigurationEditor;
  categoryId: string;
  /** Shown beside the title: the sub-tab the category belongs to, while a filter lists every tab's. */
  tag?: ReactNode;
}) {
  const headingId = `${useId()}-title`;
  const visible = editor.visibleGroups.find((candidate) => candidate.id === categoryId);
  if (!visible) return null;

  const {
    drafts,
    entryErrors,
    pendingResets,
    pendingSecrets,
    savingGroup,
    testingAlert,
    onDraftChange,
    toggleReset,
    toggleMarkSecret,
  } = editor;
  // The whole category, including entries a filter is hiding.
  const group = editor.groups.find((candidate) => candidate.id === visible.id) ?? visible;
  const channelEntries = alertWebhookEntries(group.entries);
  const dirty = editor.dirtyCount(group);
  const hidden = group.entries.length - visible.entries.length;
  const saving = savingGroup === group.id;
  const busy = saving || Boolean(channelEntries && testingAlert);
  // One save at a time: while any category saves, every bar waits.
  const barHeld = busy || savingGroup !== null;
  const groupError = editor.groupErrors[group.id];

  // What is usually set once follows the rest, under a heading of its own
  // when the category has both. The alert channel stands in for its webhooks,
  // and leads the category unless they are set once.
  const webhooks = channelEntries
    ? visible.entries.filter((entry) => isAlertWebhook(entry.key))
    : [];
  const fields = visible.entries.filter((entry) => !channelEntries || !isAlertWebhook(entry.key));
  const everyday = fields.filter((entry) => entry.one_time !== true);
  const oneTime = fields.filter((entry) => entry.one_time === true);
  const channel =
    channelEntries && webhooks.length > 0 ? (
      <AlertNotificationFields
        entries={channelEntries}
        channel={editor.alertChannel}
        onChannelChange={editor.changeAlertChannel}
        drafts={drafts}
        onDraftChange={onDraftChange}
        errors={entryErrors}
        disabled={busy}
        testing={testingAlert}
        testResult={editor.alertTestResult}
        onTest={() => void editor.sendAlertTest()}
        onReset={toggleReset}
        pendingResets={new Set(Object.keys(pendingResets))}
      />
    ) : null;
  const channelOneTime = webhooks.some((entry) => entry.one_time === true);
  const channelFirst = channel !== null && !channelOneTime;
  const oneTimeHeading =
    (everyday.length > 0 || channelFirst) && (oneTime.length > 0 || channelOneTime);
  const field = (entry: ConfigEntry) => (
    <ConfigEntryField
      key={entry.key}
      entry={entry}
      draft={drafts[entry.key]}
      onChange={onDraftChange}
      serverError={entryErrors[entry.key]}
      onReset={toggleReset}
      onMarkSecret={toggleMarkSecret}
      layout="split"
      resetPending={Boolean(pendingResets[entry.key])}
      markSecretPending={Boolean(pendingSecrets[entry.key])}
      disabled={busy}
    />
  );

  return (
    <section
      id={configSectionId(group.id)}
      aria-labelledby={headingId}
      className="scroll-mt-4 focus:outline-none"
    >
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 id={headingId} className="text-[14px] font-semibold text-gray-900">
            {group.label}
          </h2>
          {tag ? (
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600">
              {tag}
            </span>
          ) : null}
        </div>
        {group.description ? (
          <p className="mt-0.5 max-w-3xl text-[12px] text-gray-500">{group.description}</p>
        ) : null}
      </div>

      {/* A form, so Enter saves the category, and one browsers do not
          offer to remember: a secret typed here is not a login. The
          console checks the values itself, so the browser does not. */}
      <form
        autoComplete="off"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (!barHeld && dirty > 0) editor.saveGroup(group);
        }}
      >
        {dirty > 0 && hidden > 0 ? (
          <p className="mb-3 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-600">
            {hidden === group.entries.length
              ? 'Every setting in this category is hidden by the current filter.'
              : `${plural(hidden, 'setting')} in this category ${hidden === 1 ? 'is' : 'are'} hidden by the current filter.`}{' '}
            Unsaved changes to them are kept and saved with the rest.
          </p>
        ) : null}
        {liveGroupNames(group).map((name) => (
          <LiveConfigStatus
            key={name}
            groupName={name}
            refreshKey={editor.config?.source_version?.revision ?? null}
          />
        ))}
        <div className={`${PANEL} ${dirty > 0 ? 'border-gray-400 shadow-sm' : ''}`}>
          <div className="divide-y divide-gray-100 px-4">
            {channelFirst ? channel : null}
            {everyday.map(field)}
            {oneTimeHeading ? (
              <h3 className={`-mx-4 bg-gray-50 px-4 py-1.5 ${FIELD_LABEL}`}>Usually set once</h3>
            ) : null}
            {channelFirst ? null : channel}
            {oneTime.map(field)}
          </div>

          {/* Appears with the first change, or a reason to show, and stays
              at the bottom of the window while its category is on screen.
              Not the shared EditBar, whose busy state reads "Saving…": a
              test notification also holds the Alerts category, and saves
              nothing. */}
          {dirty > 0 || groupError || saving ? (
            <div className="animate-admin-reveal sticky bottom-0 z-10 rounded-b-lg border-t border-gray-200 bg-white/95 px-4 py-3 backdrop-blur">
              {groupError ? (
                <div className="mb-2">
                  <AdminNotice role="alert">{groupError}</AdminNotice>
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
                  onClick={() => editor.discardGroup(group)}
                  disabled={barHeld}
                  aria-label={`Discard changes to ${group.label}`}
                  className={BUTTON_GHOST}
                >
                  Discard
                </button>
                <button
                  type="submit"
                  disabled={barHeld || dirty === 0}
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
}

/** Add variable, and the form it opens: for a variable no category lists yet. */
export function AddVariableControl({ editor }: { editor: ConfigurationEditor }) {
  const toast = useAccountToast();
  const [adding, setAdding] = useState(false);
  if (!editor.config) return null;
  const close = () => {
    editor.closeAddForm();
    setAdding(false);
  };
  if (adding) {
    return (
      <AddVariableForm
        existingKeys={new Set(editor.config.entries.map((entry) => entry.key))}
        expectedVersion={editor.addVariableVersion}
        onCancel={close}
        onConflict={editor.reloadForAdd}
        onAdded={(next, key, base) => {
          editor.variableAdded(next, key, base);
          setAdding(false);
          toast.success(`Added ${key}.`);
        }}
      />
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-gray-50 px-3 py-2">
      <p className="text-[12px] text-gray-600">
        A <code className="rounded bg-gray-100 px-1">{'${VAR}'}</code> reference in a configuration
        file that is not listed here needs a variable of its own.
      </p>
      <button
        type="button"
        onClick={() => {
          editor.openAddForm();
          setAdding(true);
        }}
        className={BUTTON_SECONDARY}
      >
        Add variable
      </button>
    </div>
  );
}

/** Download every setting as a file, or replace them all from one. */
export function ConfigurationBackupSection({ editor }: { editor: ConfigurationEditor }) {
  const restoreInputRef = useRef<HTMLInputElement | null>(null);
  const busy = editor.backupConfirmation.busy;
  return (
    <AdminSection
      title="Backup & Restore"
      description="A backup holds the configuration, custom providers and their API keys, routes and runtime settings. Restoring one replaces all of them. The file holds API keys and secrets in plain text: store it securely."
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void editor.downloadBackup()}
          disabled={busy}
          title="Downloads every setting and provider key as JSON, with secrets in plain text"
          className={BUTTON_SECONDARY}
        >
          Backup
        </button>
        <button
          type="button"
          onClick={() => restoreInputRef.current?.click()}
          disabled={busy}
          className={BUTTON_SECONDARY}
        >
          Restore
        </button>
        <input
          ref={restoreInputRef}
          type="file"
          accept=".json,application/json"
          aria-label="Select configuration backup file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void editor.restoreFromFile(file);
          }}
          className="sr-only"
        />
      </div>
    </AdminSection>
  );
}

/** A custom variable: a `${VAR}` reference in models.yaml the registry cannot see. */
function AddVariableForm({
  existingKeys,
  expectedVersion,
  onAdded,
  onCancel,
  onConflict,
}: {
  existingKeys: ReadonlySet<string>;
  /**
   * The version the add is conditional on: the one the form opened on, moved
   * only by this page's own writes and by `onConflict`. A save elsewhere since
   * is a conflict to review, not something to write over.
   */
  expectedVersion: ConfigSourceVersion | null;
  onAdded: (next: ConfigResponse, key: string, base: ConfigSourceVersion | null) => void;
  onCancel: () => void;
  /** Show the latest configuration after a conflict; `null` when it could not be read. */
  onConflict: () => Promise<ConfigResponse | null>;
}) {
  const baseId = useId();
  // After a conflict: the latest configuration is being read, then the
  // administrator reviews it and adds again. Nothing is retried for them.
  const [reloading, setReloading] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  // The backend's rule: a name that looks like a credential's is secret
  // whatever the request says (a flag only ever adds secrecy). Any other name
  // is the administrator's choice.
  const [secretChoice, setSecretChoice] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const writes = useConfirmAction();
  const saving = writes.busy;
  const secretByName = looksSecret(name);
  const secret = secretByName || secretChoice;

  /** A conflict: nothing was written. Read the latest values and say what to do next. */
  const reviewConflict = async (key: string) => {
    setReloading(true);
    setConflicted(true);
    setError(null);
    const fresh = await onConflict();
    setReloading(false);
    if (fresh === null) {
      // The form's version stays: adding again meets the same conflict, and reads again.
      setError(
        'The configuration was changed elsewhere since this form opened, so the variable was not added, and the latest configuration could not be loaded. Your entry is kept; try adding it again.',
      );
      return;
    }
    const existing = fresh.entries.find((candidate) => candidate.key === key);
    if (existing) {
      const category =
        fresh.categories.find((candidate) => candidate.id === existing.category)?.label ??
        existing.category;
      setError(
        `${key} was added elsewhere in the meantime, so it was not added here. Edit it in its category (${category}).`,
      );
      return;
    }
    setError(
      'The configuration was changed elsewhere since this form opened, so the variable was not added. The latest configuration is shown now and your entry is kept. Review it, then add it again.',
    );
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (reloading) return;
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
    // Taken now: the version this attempt is conditional on, whatever a read
    // brings in while it is in flight.
    const base = expectedVersion;
    let conflict = false;
    // A refusal that echoes a secret's value shows it masked; the form keeps what was typed.
    const failure = await writes.perform({
      run: async () => {
        const next = await patchConfig(
          {
            values: { [key]: value },
            secrets: { [key]: secret },
          },
          ...versioned(base),
        ).catch((err: unknown) => {
          // Told apart here, while the error still has its type: the message
          // the write flow returns, and the masking below, lose it.
          if (isVersionConflict(err)) conflict = true;
          throw secret ? new Error(withoutSecret(getErrorMessage(err), value)) : err;
        });
        onAdded(next, key, base);
      },
    });
    if (conflict) {
      await reviewConflict(key);
      return;
    }
    if (failure) setError(failure);
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
            disabled={saving}
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
            disabled={saving}
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
          disabled={secretByName || saving}
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
          disabled={saving || reloading}
          aria-busy={saving || reloading || undefined}
          className={BUTTON_PRIMARY}
        >
          {saving
            ? 'Adding…'
            : reloading
              ? 'Loading latest…'
              : conflicted
                ? 'Add again'
                : 'Add variable'}
        </button>
      </div>
    </form>
  );
}
