'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { ChangeList, type Change } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import {
  UsageInsightsSettings,
  getUsageInsightsSettings,
  updateUsageInsightsSettings,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_SECONDARY,
  FORM_LABEL,
  PANEL,
  TEXT_FIELD,
} from '@/components/features/admin/adminStyles';

interface UsageInsightsSettingsSectionProps {
  onToast: (msg: string) => void;
}

/**
 * Configures the provider used by the admin Usage Insights analysis: a
 * gateway API key (stored server-side, never returned in full) and the
 * model to run the report with. The Analyze action lives on each user's detail
 * panel; this section only manages credentials.
 */
export function UsageInsightsSettingsSection({ onToast }: UsageInsightsSettingsSectionProps) {
  const [settings, setSettings] = useState<UsageInsightsSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const confirmation = useConfirmAction();
  const busy = confirmation.busy;
  const [editing, setEditing] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const baseId = useId();

  // Empty key field means "leave the stored key unchanged" on save.
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState('');

  // A reload in flight when this section goes away — a tab switch, or a test
  // environment tearing down — would otherwise land its setState on an
  // unmounted tree and throw where nothing can catch it.
  const mounted = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await getUsageInsightsSettings();
      if (!mounted.current) return;
      setSettings(s);
      setModel(s.model);
    } catch (e) {
      if (!mounted.current) return;
      setError(getErrorMessage(e));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const trimmedModel = model.trim();
  const changes: Change[] = [];
  if (settings && trimmedModel && trimmedModel !== settings.model) {
    changes.push({ label: 'Model', before: settings.model, after: trimmedModel });
  }
  // The key is write-only: the summary says what happens to it, never what it is.
  if (apiKey.trim()) {
    changes.push({
      label: 'Gateway API key',
      before: settings?.configured ? 'Set' : 'Not set',
      after: 'Replace',
    });
  }

  const cancel = () => {
    if (busy) return;
    setApiKey('');
    setModel(settings?.model ?? '');
    setFormError(null);
    setEditing(false);
  };

  const onSave = () => {
    if (!trimmedModel) {
      setFormError('Model name cannot be empty.');
      return;
    }
    if (changes.length === 0) return;
    setFormError(null);
    const patch: { api_key?: string; model?: string } = {};
    if (apiKey.trim()) patch.api_key = apiKey.trim();
    if (trimmedModel !== settings?.model) patch.model = trimmedModel;
    confirmation.request({
      title: `Save ${changes.length === 1 ? '1 change' : `${changes.length} changes`} to Usage Insights?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: <ChangeList changes={changes} />,
      run: async () => {
        const updated = await updateUsageInsightsSettings(patch);
        setSettings(updated);
        setModel(updated.model);
        setApiKey('');
        setEditing(false);
        onToast('Usage Insights provider saved');
      },
    });
  };

  const onClearKey = () => {
    confirmation.request({
      title: 'Clear the Usage Insights API key?',
      confirmLabel: 'Clear key',
      busyLabel: 'Clearing…',
      tone: 'danger',
      body: (
        <p>
          The stored key is removed, and Usage Insights analysis stops working until a new key is
          saved.
        </p>
      ),
      run: async () => {
        const updated = await updateUsageInsightsSettings({ api_key: '' });
        setSettings(updated);
        setApiKey('');
        onToast('API key cleared');
      },
    });
  };

  const configured = !!settings?.configured;

  return (
    <AdminSection
      title="Usage Insights"
      description={
        <>
          API key and model for the LLM-powered Usage Insights analysis. The analysis runs through
          this gateway, so the key is one of its own. It is stored server-side and never shown
          again. Run the analysis from a user&apos;s detail panel using the &ldquo;Analyze
          usage&rdquo; button.
        </>
      }
      actions={
        !editing && !loading && settings ? (
          <EditButton target="Usage Insights" onClick={() => setEditing(true)} />
        ) : null
      }
    >
      {error && (
        <div className="mb-3">
          <AdminNotice onDismiss={() => setError(null)}>{error}</AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : (
        <div className={`${PANEL} max-w-3xl space-y-4 p-4`}>
          <div className="flex items-center gap-2">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                configured ? 'bg-emerald-500' : 'bg-gray-300'
              }`}
            />
            <span className="text-[13px] text-gray-900">
              {configured ? (
                <>
                  Key configured{' '}
                  <span className="font-mono text-gray-500">{settings?.api_key_hint}</span>
                </>
              ) : (
                'No API key configured'
              )}
            </span>
          </div>

          {editing ? (
            <>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <label htmlFor={`${baseId}-key`} className={FORM_LABEL}>
                    Gateway API key
                  </label>
                  <input
                    id={`${baseId}-key`}
                    type="password"
                    value={apiKey}
                    disabled={busy}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={
                      configured ? 'Enter a new key to replace the stored one' : 'sk-...'
                    }
                    autoComplete="new-password"
                    className={TEXT_FIELD}
                  />
                </div>
                <div>
                  <label htmlFor={`${baseId}-model`} className={FORM_LABEL}>
                    Model
                  </label>
                  <input
                    id={`${baseId}-model`}
                    type="text"
                    value={model}
                    disabled={busy}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder="glm-5.1"
                    className={TEXT_FIELD}
                  />
                </div>
              </div>
              <EditBar
                target="Usage Insights"
                changeCount={changes.length}
                onCancel={cancel}
                onSave={onSave}
                busy={busy}
                error={formError}
              />
            </>
          ) : (
            <>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
                <dt className="text-gray-500">Model</dt>
                <dd className="text-gray-900">{settings?.model || 'Not set'}</dd>
              </dl>
              {configured ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={onClearKey}
                  className={BUTTON_SECONDARY}
                >
                  Clear key
                </button>
              ) : null}
            </>
          )}
        </div>
      )}
      {confirmation.dialog}
    </AdminSection>
  );
}
