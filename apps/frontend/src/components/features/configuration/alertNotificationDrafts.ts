import type { AlertNotificationChannel, ConfigEntry } from '@/lib/api/config';

import { APIError } from '@/lib/utils/errors';

import { buildPatch, hasStoredValue, type BuiltPatch, type ConfigDrafts } from './configDrafts';

export type AlertChannelChoice = AlertNotificationChannel | 'none';

export const ALERT_WEBHOOK_KEYS = [
  'SLACK_ALERTS_WEBHOOK_URL',
  'SLACK_WEBHOOK_URL',
  'WECOM_ALERTS_WEBHOOK_URL',
] as const;

export function isAlertWebhook(key: string): boolean {
  return ALERT_WEBHOOK_KEYS.some((candidate) => candidate === key);
}

export interface AlertWebhookEntries {
  slack: ConfigEntry;
  legacySlack: ConfigEntry;
  wecom: ConfigEntry;
}

/** Older backends keep their generic configuration fields. */
export function alertWebhookEntries(entries: readonly ConfigEntry[]): AlertWebhookEntries | null {
  const [slack, legacySlack, wecom] = ALERT_WEBHOOK_KEYS.map((key) =>
    entries.find((entry) => entry.key === key && entry.secret),
  );
  return slack && legacySlack && wecom ? { slack, legacySlack, wecom } : null;
}

export function savedAlertChannel(entries: AlertWebhookEntries): AlertChannelChoice {
  if (entries.wecom.is_set) return 'wecom';
  if (entries.slack.is_set || entries.legacySlack.is_set) return 'slack';
  return 'none';
}

/** Preserve existing Slack setups; new ones also enable the failed-request detector. */
export function selectedWebhookEntry(
  entries: AlertWebhookEntries,
  channel: AlertChannelChoice,
): ConfigEntry | null {
  if (channel === 'none') return null;
  if (channel === 'wecom') return entries.wecom;
  return entries.slack.is_set ? entries.slack : entries.legacySlack;
}

export function alertWebhookError(entry: ConfigEntry, drafts: ConfigDrafts): string | null {
  const draft = drafts[entry.key];
  if (!draft) return entry.is_set ? null : 'Enter a webhook URL before saving this channel.';
  if (draft.kind === 'clear') return 'Choose None to turn off notifications.';
  const value = String(draft.value).trim();
  if (!value) return 'Enter a webhook URL, or cancel the replacement to keep the saved one.';
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.hash) {
      return 'Enter an HTTPS webhook URL without a username, password or fragment.';
    }
  } catch {
    return 'Enter a valid HTTPS webhook URL.';
  }
  return null;
}

/** Clear the competing channel in the same validated write as its replacement. */
export function buildAlertChannelPatch(
  entries: AlertWebhookEntries,
  drafts: ConfigDrafts,
  channel: AlertChannelChoice,
  channelChanged: boolean,
): BuiltPatch {
  const selected = selectedWebhookEntry(entries, channel);
  const patch = buildPatch(selected ? [selected] : [], drafts);
  if (selected && typeof patch.values[selected.key] === 'string') {
    patch.values[selected.key] = String(patch.values[selected.key]).trim();
  }
  if (selected && (channelChanged || drafts[selected.key])) {
    const error = alertWebhookError(selected, drafts);
    if (error) patch.errors[selected.key] = error;
  }
  if (!channelChanged && Object.keys(patch.values).length === 0) return patch;
  if (channel !== 'slack') {
    patch.values[entries.slack.key] = '';
    patch.values[entries.legacySlack.key] = '';
  }
  if (channel !== 'wecom') patch.values[entries.wecom.key] = '';
  return patch;
}

/**
 * What the console says when a test notification fails.
 *
 * Only the backend's own sentences are repeated: the test endpoint never puts
 * the webhook in one, but the console cannot check that of an arbitrary proxy
 * response, and this is the one place in the page where a URL would leak back.
 * Everything else falls back to guidance chosen by status, which is what makes
 * a 503 ("try again") read differently from a 400 ("check the address").
 */
const SERVER_OWNED_TEST_REASONS = new Set([
  'Enter a webhook URL or save one for the selected channel first',
  'Enter a valid Slack incoming webhook URL',
  'Enter a valid WeCom incoming webhook URL',
  'Test notification could not be delivered; check the webhook',
]);

export function alertTestFailureMessage(error: unknown): string {
  const status = error instanceof APIError ? error.statusCode : undefined;
  if (status === 404) {
    return 'This backend does not support test notifications. Upgrade it and try again.';
  }
  if (status === 429) {
    return 'Test notifications are temporarily limited. Wait a few seconds and try again.';
  }
  if (status === 503) {
    return 'The saved webhook could not be read. This is a backend problem, not the address; try again in a moment.';
  }
  if (error instanceof APIError && SERVER_OWNED_TEST_REASONS.has(error.message)) {
    return error.message;
  }
  if (status === 400) {
    return 'The webhook address was rejected. A newly typed address must be an official HTTPS webhook for the selected channel.';
  }
  return 'The test could not be sent. Check the webhook URL and try again.';
}

/**
 * The webhook entries that still have a database row but are not the one the
 * card is editing.
 *
 * Choosing None, or switching channel, stores an empty string for the channels
 * it turns off — that is deliberate, because a deleted row would let an
 * environment value turn the channel back on. The cost is that the empty row
 * also hides the environment from then on, so these are exactly the settings an
 * administrator needs a Reset for: it removes the row and restores the
 * environment.
 */
export function resettableAlertWebhooks(
  entries: AlertWebhookEntries,
  selected: ConfigEntry | null,
): ConfigEntry[] {
  return [entries.slack, entries.legacySlack, entries.wecom].filter(
    (entry) => entry.key !== selected?.key && hasStoredValue(entry) && !entry.immutable,
  );
}
