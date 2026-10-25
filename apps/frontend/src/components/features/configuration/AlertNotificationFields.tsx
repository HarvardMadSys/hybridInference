'use client';

import { useId } from 'react';

import { BUTTON_SECONDARY } from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import { ConfigEntryField } from './ConfigEntryField';
import { AlertChannelPicker } from './AlertChannelPicker';
import {
  alertWebhookError,
  resettableAlertWebhooks,
  selectedWebhookEntry,
  type AlertChannelChoice,
  type AlertWebhookEntries,
} from './alertNotificationDrafts';
import type { ConfigDraft, ConfigDrafts } from './configDrafts';

export type AlertTestResult = { sent: true; testId: string } | { sent: false; message: string };

export function AlertNotificationFields({
  entries,
  channel,
  onChannelChange,
  drafts,
  onDraftChange,
  errors,
  disabled,
  testing,
  testResult,
  onTest,
  onReset,
}: {
  entries: AlertWebhookEntries;
  channel: AlertChannelChoice;
  onChannelChange: (channel: AlertChannelChoice) => void;
  drafts: ConfigDrafts;
  onDraftChange: (key: string, draft: ConfigDraft | undefined) => void;
  errors: Record<string, string>;
  disabled: boolean;
  testing: boolean;
  testResult: AlertTestResult | null;
  onTest: () => void;
  /** Remove a stored row, so the setting follows the environment again. */
  onReset: (entry: ConfigEntry) => void;
}) {
  const id = useId();
  const entry = selectedWebhookEntry(entries, channel);
  const name = channel === 'wecom' ? 'WeCom' : 'Slack';
  const pending = Object.values(entries).some((candidate) => candidate.pending_restart);
  // Channels turned off by an earlier save keep an empty row, and that row wins
  // over the environment forever. Resetting one removes the row; without that
  // button the only way back to a .env webhook was SQL or the config CLI. Only
  // an *empty* row is listed: a channel that still has a value is cleared by
  // the save in progress, and a Reset there would race it.
  const storedElsewhere = resettableAlertWebhooks(entries, entry).filter(
    (candidate) => !candidate.is_set,
  );

  return (
    // Laid out like the other settings in the list: name and description
    // beside the editor on wide screens.
    <div className="py-5 lg:grid lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] lg:gap-8">
      <div className="mb-3 lg:mb-0">
        <label htmlFor={id} className="text-[13px] font-medium text-gray-900">
          Notification channel
        </label>
        <p id={`${id}-description`} className="mt-1 text-[12px] text-gray-500">
          Choose one channel for backend alerts.
        </p>
      </div>
      <div className="min-w-0 space-y-4 lg:max-w-2xl">
        <AlertChannelPicker
          id={id}
          value={channel}
          onChange={onChannelChange}
          disabled={disabled}
          describedBy={`${id}-description ${id}-restart`}
        />

        {entry ? (
          <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4">
            <ConfigEntryField
              entry={{
                ...entry,
                description:
                  channel === 'wecom'
                    ? 'Custom message push URL from your WeCom group. Saved URLs are never shown.'
                    : 'Incoming webhook URL for your Slack channel. Saved URLs are never shown.',
              }}
              label={`${name} webhook URL`}
              allowSecretClear={false}
              presentation="compact"
              draft={drafts[entry.key]}
              onChange={onDraftChange}
              serverError={errors[entry.key]}
              onReset={onReset}
              disabled={disabled}
            />
            <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-gray-200/70 pt-3">
              <button
                type="button"
                onClick={onTest}
                disabled={disabled || Boolean(alertWebhookError(entry, drafts))}
                aria-busy={testing || undefined}
                className={BUTTON_SECONDARY}
              >
                {testing ? 'Sending test…' : 'Send test notification'}
              </button>
              <p className="text-[11px] text-gray-500">Sends a test without saving changes.</p>
            </div>
            {testResult ? (
              <p
                role={testResult.sent ? 'status' : 'alert'}
                className={`mt-3 rounded-lg px-3 py-2 text-[12px] ${testResult.sent ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-600'}`}
              >
                {testResult.sent
                  ? `Test sent. Check your ${name} channel for ${testResult.testId}.`
                  : testResult.message}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="rounded-xl border border-dashed border-gray-200 bg-gray-50/50 px-4 py-3 text-[12px] text-gray-500">
            Select Slack or WeCom to configure a webhook.
          </p>
        )}
        {storedElsewhere.length > 0 ? (
          <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4">
            <p className="text-[12px] text-gray-600">
              {channel === 'none'
                ? 'Turning notifications off stored an empty value for the channel that was on. An empty stored value also hides any webhook set in .env.'
                : 'Switching channel stored an empty value for the previous channel. An empty stored value also hides any webhook set in .env.'}{' '}
              Resetting removes the stored value, so the setting follows .env again on the next
              restart.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {storedElsewhere.map((candidate) => (
                <button
                  key={candidate.key}
                  type="button"
                  onClick={() => onReset(candidate)}
                  disabled={disabled}
                  aria-label={`Reset ${candidate.key}`}
                  className="rounded-md border border-gray-200 bg-white px-3 py-1.5 font-mono text-[11px] font-medium text-gray-700 shadow-sm transition hover:border-gray-300 hover:bg-gray-50 disabled:opacity-40"
                >
                  Reset {candidate.key}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        <p
          id={`${id}-restart`}
          className={`text-[11px] ${pending ? 'text-amber-700' : 'text-gray-500'}`}
        >
          {pending ? (
            <>
              <span className="font-medium">Pending restart</span> — saved changes have not taken
              effect.
            </>
          ) : (
            'Changes apply after saving and restarting the backend.'
          )}
        </p>
      </div>
    </div>
  );
}
