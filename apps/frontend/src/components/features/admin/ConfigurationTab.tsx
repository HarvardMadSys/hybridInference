'use client';

import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import toast from 'react-hot-toast';

import { ConfigEntryField } from '@/components/features/configuration/ConfigEntryField';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
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
  const [adding, setAdding] = useState(false);

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
  }, []);

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
            (entry) => (!missingOnly || entry.missing) && matchesQuery(entry, query, group.label),
          ),
        }))
        .filter((group) => group.entries.length > 0),
    [groups, missingOnly, query],
  );

  const saveGroup = async (group: ConfigGroup) => {
    // The whole category, including entries a filter is hiding: a change the
    // administrator made stays part of the save it was made for.
    const { values, errors } = buildPatch(group.entries, drafts);
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
    const keys = group.entries.map((entry) => entry.key);
    setDrafts((prev) => withoutKeys(prev, keys));
    setEntryErrors((prev) => withoutKeys(prev, keys));
    setGroupErrors((prev) => withoutKeys(prev, [group.id]));
  };

  const confirmReset = async () => {
    if (!resetTarget) return;
    const { key, custom } = resetTarget;
    setResetting(true);
    setResetError(null);
    try {
      const next = await resetConfigKey(key);
      applyResponse(next, [key]);
      setResetTarget(null);
      toast.success(custom ? `Deleted ${key}.` : `Reset ${key}.`);
    } catch (e) {
      setResetError(getErrorMessage(e));
    } finally {
      setResetting(false);
    }
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
        const dirty = countDirty(group.entries, drafts);
        const saving = savingGroup === group.id;
        const headingId = `${searchId}-${group.id}`;
        return (
          <section
            key={group.id}
            aria-labelledby={headingId}
            className="rounded-xl border border-gray-200 bg-white p-5"
          >
            <h2 id={headingId} className="text-[14px] font-semibold text-gray-900">
              {group.label}
            </h2>
            {group.description ? (
              <p className="mt-1 text-[12px] text-gray-500">{group.description}</p>
            ) : null}

            {groupErrors[group.id] ? (
              <div
                className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600"
                role="alert"
              >
                {groupErrors[group.id]}
              </div>
            ) : null}

            <div className="mt-1 divide-y divide-gray-100">
              {visible.entries.map((entry) => (
                <ConfigEntryField
                  key={entry.key}
                  entry={entry}
                  draft={drafts[entry.key]}
                  onChange={onDraftChange}
                  serverError={entryErrors[entry.key]}
                  onReset={(target) => {
                    setResetError(null);
                    setResetTarget(target);
                  }}
                  disabled={saving}
                />
              ))}
            </div>

            {dirty > 0 ? (
              <div className="mt-2 flex flex-wrap items-center justify-end gap-2 border-t border-gray-100 pt-3">
                <span className="mr-auto text-[12px] text-gray-500">
                  {plural(dirty, 'unsaved change')}
                </span>
                <button
                  type="button"
                  onClick={() => discardGroup(group)}
                  disabled={saving}
                  className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-40"
                >
                  Discard
                </button>
                <button
                  type="button"
                  onClick={() => void saveGroup(group)}
                  disabled={saving}
                  aria-busy={saving || undefined}
                  aria-label={`Save ${group.label}`}
                  className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
              </div>
            ) : null}
          </section>
        );
      })}

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
  // Follows the name (the backend's own rule) until the administrator decides.
  const [secretChoice, setSecretChoice] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const secret = secretChoice ?? looksSecret(name);

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
    <form
      onSubmit={(event) => void submit(event)}
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
            data-1p-ignore
            data-lpignore="true"
            className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
          />
        </div>
      </div>
      <label className="mt-3 flex items-center gap-1.5 text-[13px] text-gray-700">
        <input
          type="checkbox"
          checked={secret}
          onChange={(event) => setSecretChoice(event.target.checked)}
          className="h-4 w-4 rounded border-gray-300"
        />
        Secret (write-only; never shown again)
      </label>
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
          {saving ? 'Adding…' : 'Add'}
        </button>
      </div>
    </form>
  );
}
