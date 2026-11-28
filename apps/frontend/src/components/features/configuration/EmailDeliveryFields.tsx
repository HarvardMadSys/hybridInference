'use client';

import { useId, useState } from 'react';

import {
  BUTTON_SECONDARY,
  FIELD_INVALID,
  TEXT_FIELD,
} from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import { ConfigEntryField } from './ConfigEntryField';
import { settleDraft, type ConfigDraft, type ConfigDrafts } from './configDrafts';
import {
  EMAIL_PROVIDERS,
  emailDeliveryParts,
  emailFieldEntry,
  emailFieldLabel,
  emailProviderLabel,
  emailRequired,
  looksLikeEmailAddress,
  savedEmailStatus,
  selectedEmailProvider,
  senderWarning,
  type EmailTestResult,
} from './emailDeliveryDrafts';
import { NOT_A_LOGIN_FIELD } from './notALoginField';

/** Sending a test: only on Admin → Settings, where the settings are saved first. */
export interface EmailTestControls {
  testing: boolean;
  result: EmailTestResult | null;
  /** Unsaved changes in the category: the test uses the saved settings, so it waits. */
  unsaved: boolean;
  /** `to` is empty to send to the administrator's own address. */
  onTest: (to: string) => void;
}

const ROW_SPLIT = 'py-4 lg:grid lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] lg:gap-8';

/**
 * The Email category as one card: which service sends the gateway's mail,
 * that service's settings under the names an administrator looks for, the
 * sender, and — on the Settings tab — a test through the saved settings.
 *
 * `entries` is the whole category, the settings of the transports not chosen
 * included: the card shows the chosen one's, and reads the saved choice's to
 * say whether email works now. Edits are ordinary drafts, saved with the
 * category.
 */
export function EmailDeliveryFields({
  entries,
  drafts,
  onDraftChange,
  errors,
  disabled,
  layout = 'split',
  showStatus = true,
  test,
  onReset,
  onMarkSecret,
  pendingResets,
  pendingSecrets,
}: {
  entries: readonly ConfigEntry[];
  drafts: ConfigDrafts;
  onDraftChange: (key: string, draft: ConfigDraft | undefined) => void;
  errors: Record<string, string>;
  disabled: boolean;
  layout?: 'split' | 'stacked';
  /** Say whether the saved settings send, and why they are required: not during setup. */
  showStatus?: boolean;
  test?: EmailTestControls;
  onReset?: (entry: ConfigEntry) => void;
  onMarkSecret?: (entry: ConfigEntry) => void;
  pendingResets?: ReadonlySet<string>;
  pendingSecrets?: ReadonlySet<string>;
}) {
  const id = useId();
  const parts = emailDeliveryParts(entries, drafts);
  if (!parts) return null;
  const provider = selectedEmailProvider(parts.provider, drafts);
  const warning = senderWarning(entries, drafts, provider);

  const field = (entry: ConfigEntry) => (
    <ConfigEntryField
      key={entry.key}
      entry={emailFieldEntry(entry, provider)}
      label={emailFieldLabel(entry.key)}
      showKey
      layout={layout}
      draft={drafts[entry.key]}
      onChange={onDraftChange}
      serverError={errors[entry.key]}
      onReset={onReset}
      onMarkSecret={onMarkSecret}
      resetPending={pendingResets?.has(entry.key) ?? false}
      markSecretPending={pendingSecrets?.has(entry.key) ?? false}
      disabled={disabled}
    />
  );

  return (
    <>
      {showStatus ? <EmailStatus entries={entries} /> : null}

      <div className={layout === 'split' ? ROW_SPLIT : 'py-4'} data-config-key={parts.provider.key}>
        <div className="mb-3 lg:mb-0">
          <p id={`${id}-provider-label`} className="text-[13px] font-medium text-gray-900">
            Delivery service{' '}
            <span className="font-mono text-[11px] font-normal text-gray-400">
              {parts.provider.key}
            </span>
          </p>
          <p id={`${id}-provider-description`} className="mt-0.5 text-[12px] text-gray-500">
            How the gateway sends verification, password-reset and notification emails. Only the
            chosen service&apos;s settings are used.
          </p>
        </div>
        <ProviderPicker
          name={`${id}-provider`}
          entry={parts.provider}
          value={provider}
          labelledBy={`${id}-provider-label`}
          describedBy={`${id}-provider-description`}
          disabled={disabled}
          onChange={(next) =>
            onDraftChange(
              parts.provider.key,
              settleDraft(parts.provider, { kind: 'value', value: next }),
            )
          }
        />
      </div>

      {parts.transport.map(field)}
      {parts.sender.map(field)}
      {warning ? (
        <p role="note" className="mb-4 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
          {warning}
        </p>
      ) : null}

      {test ? <EmailTestRow test={test} disabled={disabled} layout={layout} /> : null}
    </>
  );
}

function ProviderPicker({
  name,
  entry,
  value,
  labelledBy,
  describedBy,
  disabled,
  onChange,
}: {
  name: string;
  entry: ConfigEntry;
  value: string;
  labelledBy: string;
  describedBy: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const choices = entry.choices ?? [];
  // A saved value no loaded transport answers to still shows, and cannot be chosen again.
  const options = choices.includes(value) || !value ? choices : [...choices, value];
  return (
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="grid min-w-0 gap-2 sm:grid-cols-2 lg:max-w-2xl"
    >
      {options.map((choice) => {
        const known = choices.includes(choice);
        const checked = choice === value;
        const info = EMAIL_PROVIDERS[choice];
        return (
          <label
            key={choice}
            className={`flex gap-3 rounded-lg border p-3 transition ${
              checked
                ? 'border-gray-900 bg-gray-50 ring-1 ring-gray-900'
                : 'border-gray-200 hover:border-gray-300'
            } ${disabled || !known ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
          >
            <input
              type="radio"
              name={name}
              value={choice}
              checked={checked}
              disabled={disabled || !known}
              onChange={() => onChange(choice)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-gray-900"
            />
            <span className="min-w-0">
              <span className="block text-[13px] font-medium text-gray-900">
                {info?.label ?? choice}
              </span>
              <span className="mt-0.5 block text-[12px] text-gray-500">
                {known
                  ? (info?.description ?? 'Provided by a backend extension.')
                  : 'Not available: no loaded extension provides it. Choose another service.'}
              </span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

/** Whether the saved settings send now, and why it matters. */
function EmailStatus({ entries }: { entries: readonly ConfigEntry[] }) {
  const status = savedEmailStatus(entries);
  if (!status) return null;
  const required = emailRequired(entries);
  const service = emailProviderLabel(status.provider);

  let tone: string;
  let headline: string;
  if (status.kind === 'ready') {
    tone = 'bg-emerald-50 text-emerald-800';
    headline = `Email is set up: the saved settings send through ${service}. Send a test below to check them.`;
  } else if (status.kind === 'unknown') {
    tone = 'bg-gray-50 text-gray-700';
    headline = `The saved settings send through ${service}. Send a test below to check them.`;
  } else {
    tone = required ? 'bg-red-50 text-red-800' : 'bg-amber-50 text-amber-800';
    headline = `Email is not set up yet: ${service} still needs ${listed(status.missing)}.`;
  }

  return (
    <div className="py-4" role="status">
      <div className={`rounded-lg px-3 py-2 text-[12px] ${tone}`}>
        <p className="font-medium">{headline}</p>
        {status.kind === 'incomplete' ? (
          <p className="mt-1">
            {required
              ? 'Public sign-up requires email verification, so new accounts cannot sign in until email works. To run without email, turn off Require email verification on the Sign-up tab.'
              : 'Nothing requires it now, but without email users cannot reset a forgotten password themselves.'}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function listed(names: readonly string[]): string {
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function EmailTestRow({
  test,
  disabled,
  layout,
}: {
  test: EmailTestControls;
  disabled: boolean;
  layout: 'split' | 'stacked';
}) {
  const id = useId();
  const [to, setTo] = useState('');
  const typed = to.trim();
  const invalid = typed !== '' && !looksLikeEmailAddress(typed);
  const result = test.result;

  return (
    <div className={layout === 'split' ? ROW_SPLIT : 'py-4'}>
      <div className="mb-3 lg:mb-0">
        <p className="text-[13px] font-medium text-gray-900">Test email</p>
        <p className="mt-0.5 text-[12px] text-gray-500">
          Sends a short fixed message through the saved settings, so you know email works before
          users rely on it.
        </p>
      </div>
      <div className="min-w-0 space-y-2 lg:max-w-2xl">
        <label htmlFor={`${id}-to`} className="block text-[12px] font-medium text-gray-700">
          Send to
        </label>
        <div className="flex flex-wrap items-start gap-2">
          <input
            id={`${id}-to`}
            type="email"
            inputMode="email"
            autoComplete="off"
            spellCheck={false}
            {...NOT_A_LOGIN_FIELD}
            value={to}
            onChange={(event) => setTo(event.target.value)}
            placeholder="Your account's email address"
            disabled={disabled}
            aria-invalid={invalid || undefined}
            aria-describedby={`${id}-note`}
            className={`${invalid ? `${TEXT_FIELD} ${FIELD_INVALID}` : TEXT_FIELD} min-w-0 flex-1 sm:max-w-xs`}
          />
          <button
            type="button"
            onClick={() => test.onTest(typed)}
            disabled={disabled || test.testing || test.unsaved || invalid}
            aria-busy={test.testing || undefined}
            aria-describedby={`${id}-note`}
            className={BUTTON_SECONDARY}
          >
            {test.testing ? 'Sending test…' : 'Send test email'}
          </button>
        </div>
        <p
          id={`${id}-note`}
          className={`text-[11px] ${test.unsaved || invalid ? 'text-amber-700' : 'text-gray-500'}`}
        >
          {invalid
            ? 'Enter a complete email address, or leave it empty to use your own.'
            : test.unsaved
              ? 'Save your changes first: the test uses the saved settings.'
              : 'Leave it empty to send the test to your own account.'}
        </p>
        {result ? (
          <p
            role={result.sent ? 'status' : 'alert'}
            className={`rounded-lg px-3 py-2 text-[12px] ${result.sent ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}
          >
            {result.sent
              ? `Test email sent to ${result.to} through ${emailProviderLabel(result.provider)}. If it does not arrive in a few minutes, check the spam folder.`
              : result.message}
          </p>
        ) : null}
      </div>
    </div>
  );
}
