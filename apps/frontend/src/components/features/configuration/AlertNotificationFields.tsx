'use client';

import { useId } from 'react';

import { ConfigEntryField } from './ConfigEntryField';
import { AlertChannelPicker } from './AlertChannelPicker';
import {
  alertWebhookError,
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
}) {
  const id = useId();
  const entry = selectedWebhookEntry(entries, channel);
  const name = channel === 'wecom' ? 'WeCom' : 'Slack';
  const pending = Object.values(entries).some((candidate) => candidate.pending_restart);

  return (
    <div className="space-y-4 py-5">
      <div>
        <div className="mb-3">
          <label htmlFor={id} className="text-[13px] font-medium text-gray-900">
            Notification channel
          </label>
          <p id={`${id}-description`} className="mt-1 text-[12px] text-gray-500">
            Choose one channel for backend alerts.
          </p>
        </div>
        <AlertChannelPicker
          id={id}
          value={channel}
          onChange={onChannelChange}
          disabled={disabled}
          describedBy={`${id}-description ${id}-restart`}
        />
      </div>

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
            disabled={disabled}
          />
          <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-gray-200/70 pt-3">
            <button
              type="button"
              onClick={onTest}
              disabled={disabled || Boolean(alertWebhookError(entry, drafts))}
              aria-busy={testing || undefined}
              className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[12px] font-medium text-gray-700 shadow-sm transition hover:border-gray-300 hover:bg-gray-50 disabled:opacity-40"
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
  );
}
