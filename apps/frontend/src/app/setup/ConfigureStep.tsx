'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { ConfigEntryField } from '@/components/features/configuration/ConfigEntryField';
import {
  buildPatch,
  errorForKey,
  groupEntries,
  withoutKeys,
  type ConfigDraft,
  type ConfigDrafts,
} from '@/components/features/configuration/configDrafts';
import { Button } from '@/components/ui/Button';
import { listRuntimeSettings, updateRuntimeSetting } from '@/lib/api/admin';
import { getConfig, patchConfig, type ConfigResponse } from '@/lib/api/config';
import { getErrorMessage } from '@/lib/utils/errors';

/** The two runtime settings that decide who can create an account. */
const SIGNUP_SETTINGS = [
  {
    key: 'signup_enabled',
    label: 'Allow public sign-up',
    fallbackDescription: 'Anyone can create an account from the sign-up page.',
  },
  {
    key: 'signup_require_email_verification',
    label: 'Require email verification',
    fallbackDescription: 'New accounts confirm their email address before they can sign in.',
  },
] as const;

type SignupKey = (typeof SIGNUP_SETTINGS)[number]['key'];
type SignupValues = Partial<Record<SignupKey, boolean>>;

/**
 * Step 2: who may sign up, then the settings this deployment needs now — every
 * entry the registry marks for setup, and anything required that has no value.
 * Sign-up comes first on the page and in the save, because it decides whether
 * the email (SMTP) settings are required. One "Save and continue" writes the
 * sign-up settings, then the configuration batch.
 */
export function ConfigureStep({ onSaved }: { onSaved: (config: ConfigResponse) => void }) {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [signup, setSignup] = useState<SignupValues>({});
  const [signupDescriptions, setSignupDescriptions] = useState<Partial<Record<SignupKey, string>>>(
    {},
  );
  const [signupDraft, setSignupDraft] = useState<SignupValues>({});
  const [drafts, setDrafts] = useState<ConfigDrafts>({});
  const [entryErrors, setEntryErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    const [configResult, settingsResult] = await Promise.allSettled([
      getConfig(),
      listRuntimeSettings(),
    ]);
    if (configResult.status === 'rejected') {
      setLoadError(getErrorMessage(configResult.reason));
      return;
    }
    // The sign-up switches are a convenience here (they also live on the
    // Settings tab), so a failure to read them hides them rather than the step.
    if (settingsResult.status === 'fulfilled') {
      const values: SignupValues = {};
      const descriptions: Partial<Record<SignupKey, string>> = {};
      for (const { key } of SIGNUP_SETTINGS) {
        const item = settingsResult.value.settings.find((setting) => setting.key === key);
        if (item) {
          values[key] = item.value === true;
          descriptions[key] = item.description;
        }
      }
      setSignup(values);
      setSignupDraft(values);
      setSignupDescriptions(descriptions);
    }
    setConfig(configResult.value);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The entries this step shows are fixed when it loads; a save moves on.
  const groups = useMemo(() => {
    if (!config) return [];
    const wanted = config.entries.filter((entry) => entry.setup || entry.missing);
    return groupEntries(config.categories, wanted);
  }, [config]);

  const onDraftChange = useCallback((key: string, draft: ConfigDraft | undefined) => {
    setDrafts((prev) => {
      const next = { ...prev };
      if (draft) next[key] = draft;
      else delete next[key];
      return next;
    });
    setEntryErrors((prev) => withoutKeys(prev, [key]));
  }, []);

  const save = async () => {
    const entries = groups.flatMap((group) => group.entries);
    const { values, errors } = buildPatch(entries, drafts);
    if (Object.keys(errors).length > 0) {
      setEntryErrors((prev) => ({ ...prev, ...errors }));
      setSaveError('Some values are not valid. Correct them and save again.');
      return;
    }

    setSaving(true);
    setSaveError(null);
    const keys = Object.keys(values);
    try {
      for (const { key } of SIGNUP_SETTINGS) {
        const wanted = signupDraft[key];
        if (wanted === undefined || wanted === signup[key]) continue;
        const updated = await updateRuntimeSetting(key, wanted);
        // Saved: a retry after a later failure must not send it again.
        setSignup((prev) => ({ ...prev, [key]: updated.value === true }));
      }
      // The configuration response is read after the sign-up settings were
      // written, so its `missing` list already reflects them.
      const next = keys.length > 0 ? await patchConfig({ values }) : await getConfig();
      onSaved(next);
    } catch (e) {
      const message = getErrorMessage(e);
      const keyed = errorForKey(message, keys);
      if (keyed) setEntryErrors((prev) => ({ ...prev, [keyed.key]: keyed.reason }));
      setSaveError(message);
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <div className="space-y-3">
        <div
          className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          {loadError}
        </div>
        <Button type="button" variant="secondary" onClick={() => void load()}>
          Try again
        </Button>
      </div>
    );
  }

  if (!config) {
    return (
      <p className="py-6 text-center text-sm text-gray-400" role="status">
        Loading the configuration…
      </p>
    );
  }

  const signupKeys = SIGNUP_SETTINGS.filter(({ key }) => signupDraft[key] !== undefined);

  return (
    <div className="space-y-6">
      {signupKeys.length > 0 ? (
        <section aria-labelledby="setup-group-signup">
          <h2
            id="setup-group-signup"
            className="text-base font-semibold tracking-tight text-gray-900"
          >
            Sign-up
          </h2>
          <p className="mt-0.5 text-[13px] text-gray-500">
            With both on, new users verify their email, so the email (SMTP) settings below become
            required.
          </p>
          <div className="mt-1 divide-y divide-gray-100">
            {signupKeys.map(({ key, label, fallbackDescription }) => {
              const on = signupDraft[key] === true;
              const descriptionId = `setup-signup-${key}-description`;
              return (
                <div key={key} className="flex items-center justify-between gap-4 py-4">
                  <div>
                    <p className="text-[13px] font-medium text-gray-900">{label}</p>
                    <p id={descriptionId} className="mt-0.5 text-[12px] text-gray-500">
                      {signupDescriptions[key] || fallbackDescription}
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={on}
                    aria-label={label}
                    aria-describedby={descriptionId}
                    disabled={saving}
                    onClick={() => setSignupDraft((prev) => ({ ...prev, [key]: !on }))}
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-40 ${
                      on ? 'bg-gray-900' : 'bg-gray-200'
                    }`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                        on ? 'translate-x-5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {groups.length === 0 ? (
        <p className="text-sm text-gray-600">
          Nothing else is required right now. You can change any setting later on the Configuration
          tab.
        </p>
      ) : (
        groups.map((group) => (
          <section key={group.id} aria-labelledby={`setup-group-${group.id}`}>
            <h2
              id={`setup-group-${group.id}`}
              className="text-base font-semibold tracking-tight text-gray-900"
            >
              {group.label}
            </h2>
            {group.description ? (
              <p className="mt-0.5 text-[13px] text-gray-500">{group.description}</p>
            ) : null}
            <div className="mt-1 divide-y divide-gray-100">
              {group.entries.map((entry) => (
                <ConfigEntryField
                  key={entry.key}
                  entry={entry}
                  draft={drafts[entry.key]}
                  onChange={onDraftChange}
                  serverError={entryErrors[entry.key]}
                  disabled={saving}
                />
              ))}
            </div>
          </section>
        ))
      )}

      {saveError ? (
        <div
          className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          {saveError}
        </div>
      ) : null}

      <div className="flex justify-end">
        <Button type="button" onClick={() => void save()} isLoading={saving}>
          Save and continue
        </Button>
      </div>
    </div>
  );
}
