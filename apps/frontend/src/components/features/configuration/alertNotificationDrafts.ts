import type { AlertNotificationChannel, ConfigEntry } from '@/lib/api/config';

import { buildPatch, type BuiltPatch, type ConfigDrafts } from './configDrafts';

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
