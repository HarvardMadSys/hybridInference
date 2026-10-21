'use client';

import { useId } from 'react';

import { ConfigEntryField } from './ConfigEntryField';
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
    <div className="py-4">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={id} className="text-[13px] font-medium text-gray-900">
          Notification channel
        </label>
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
          Restart required
        </span>
        {pending ? (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
            Pending restart
          </span>
        ) : null}
      </div>
      <p id={`${id}-description`} className="mt-1 text-[12px] text-gray-500">
        Choose one channel for backend alert notifications. Saved changes take effect after the
        backend restarts; until then, the current channel stays active.
      </p>
      <select
        id={id}
        value={channel}
        onChange={(event) => onChannelChange(event.target.value as AlertChannelChoice)}
        disabled={disabled}
        aria-describedby={`${id}-description`}
        className="mt-3 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-[13px] text-gray-900 disabled:opacity-50 sm:max-w-xs"
      >
        <option value="none">None</option>
        <option value="slack">Slack</option>
        <option value="wecom">WeCom (企业微信)</option>
      </select>

      {entry ? (
        <>
          <ConfigEntryField
            entry={{
              ...entry,
              description:
                channel === 'wecom'
                  ? 'Create a custom message push in your WeCom group and copy its webhook URL.'
                  : 'Use the incoming webhook URL for the Slack channel that receives alerts.',
            }}
            label={`${name} webhook URL`}
            allowSecretClear={false}
            draft={drafts[entry.key]}
            onChange={onDraftChange}
            serverError={errors[entry.key]}
            disabled={disabled}
          />
          <button
            type="button"
            onClick={onTest}
            disabled={disabled || Boolean(alertWebhookError(entry, drafts))}
            aria-busy={testing || undefined}
            className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
          >
            {testing ? 'Sending test…' : 'Send test notification'}
          </button>
          <p className="mt-2 text-[11px] text-gray-500">
            Sends one test message using the URL entered here, or the saved URL. This does not save
            changes or turn on alerting.
          </p>
          {testResult ? (
            <p
              role={testResult.sent ? 'status' : 'alert'}
              className={`mt-2 text-[12px] ${testResult.sent ? 'text-emerald-700' : 'text-red-600'}`}
            >
              {testResult.sent
                ? `Test sent. Check your ${name} channel for ${testResult.testId}.`
                : testResult.message}
            </p>
          ) : null}
        </>
      ) : (
        <p className="mt-3 text-[12px] text-gray-500">
          Backend alert notifications will be off after saving and restarting.
        </p>
      )}
    </div>
  );
}
