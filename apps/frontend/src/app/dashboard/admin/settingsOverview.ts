/**
 * What Admin → Settings → Overview says about a gateway: one line for each
 * thing an administrator sets up — its address, email, sign-up, API access
 * and alerts — with how it stands and the tab that changes it. Read from what
 * the page has already loaded; a gateway that does not report a setting gets
 * no line for it.
 */

import type { RuntimeSettingItem } from '@/lib/api/admin';
import type { ConfigEntry } from '@/lib/api/config';
import {
  alertWebhookEntries,
  savedAlertChannel,
} from '@/components/features/configuration/alertNotificationDrafts';
import {
  emailProviderLabel,
  emailRequired,
  savedEmailStatus,
} from '@/components/features/configuration/emailDeliveryDrafts';

import type { SettingsTabKey } from './settingsLayout';

/** `ok`: set up. `attention`: worth a look. `off`: deliberately not in use. */
export type OverviewTone = 'ok' | 'attention' | 'off';

export interface OverviewItem {
  id: 'site' | 'email' | 'signup' | 'api-access' | 'alerts';
  title: string;
  tone: OverviewTone;
  /** How it stands, in a word or two. */
  status: string;
  /** What that means, in a sentence. */
  detail: string;
  /** The tab that changes it. */
  tab: SettingsTabKey;
  /** The section on that tab, when it is not the first. */
  hash?: string;
}

function find(entries: readonly ConfigEntry[], key: string): ConfigEntry | undefined {
  return entries.find((entry) => entry.key === key);
}

/** "a", "a and b", "a, b and c". */
function listed(items: readonly string[]): string {
  return items.length <= 1
    ? items.join('')
    : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** A setting's value in force as text; empty when it has none. */
function text(entry: ConfigEntry | undefined): string {
  if (!entry || entry.secret) return '';
  const value = entry.value ?? entry.default;
  return value === null || value === undefined ? '' : String(value).trim();
}

/** A runtime switch's value, or `undefined` when the gateway has no such switch. */
function runtimeFlag(runtime: readonly RuntimeSettingItem[], key: string): boolean | undefined {
  const setting = runtime.find((candidate) => candidate.key === key);
  return setting && typeof setting.value === 'boolean' ? setting.value : undefined;
}

function siteItem(entries: readonly ConfigEntry[]): OverviewItem | null {
  const name = find(entries, 'SITE_NAME');
  const api = find(entries, 'BASE_URL');
  if (!name && !api) return null;
  const base = { id: 'site', title: 'Site address', tab: 'general' } as const;
  if (api && !text(api)) {
    return {
      ...base,
      tone: 'attention',
      status: 'Not set',
      detail:
        'Without the public API address, links in emails use the address each request arrived at, which is plain http behind a TLS proxy.',
    };
  }
  const address = text(find(entries, 'FRONTEND_URL')) || text(api);
  const siteName = text(name);
  return {
    ...base,
    tone: 'ok',
    status: 'Set',
    detail:
      siteName && address
        ? `${siteName}, at ${address}.`
        : address
          ? `The console is at ${address}.`
          : `The site is named ${siteName}.`,
  };
}

function emailItem(entries: readonly ConfigEntry[]): OverviewItem | null {
  const status = savedEmailStatus(entries);
  if (!status) return null;
  const base = { id: 'email', title: 'Email', tab: 'email' } as const;
  const service = emailProviderLabel(status.provider);
  if (status.kind === 'incomplete') {
    return {
      ...base,
      tone: 'attention',
      status: 'Not set up',
      detail: `${service} still needs ${listed(status.missing)}. ${
        emailRequired(entries)
          ? 'New accounts cannot verify their email until it works.'
          : 'Until it works, users cannot reset a forgotten password themselves.'
      }`,
    };
  }
  return { ...base, tone: 'ok', status: 'Ready', detail: `Mail is sent through ${service}.` };
}

function signupItem(
  entries: readonly ConfigEntry[],
  runtime: readonly RuntimeSettingItem[],
): OverviewItem | null {
  const open = runtimeFlag(runtime, 'signup_enabled');
  if (open === undefined) return null;
  const base = { id: 'signup', title: 'Sign-up', tab: 'signup' } as const;
  if (!open) {
    return { ...base, tone: 'off', status: 'Closed', detail: 'New users cannot sign up.' };
  }
  const verify = runtimeFlag(runtime, 'signup_require_email_verification');
  if (verify && savedEmailStatus(entries)?.kind === 'incomplete') {
    return {
      ...base,
      tone: 'attention',
      status: 'Open',
      detail:
        'New users must verify their email, but email is not set up yet, so they cannot finish signing up.',
    };
  }
  return {
    ...base,
    tone: 'ok',
    status: 'Open',
    detail:
      verify === false
        ? 'New users may sign up, without verifying their email.'
        : verify
          ? 'New users may sign up, and verify their email first.'
          : 'New users may sign up.',
  };
}

function apiAccessItem(runtime: readonly RuntimeSettingItem[]): OverviewItem | null {
  const required = runtimeFlag(runtime, 'user_auth_enabled');
  if (required === undefined) return null;
  const base = { id: 'api-access', title: 'API access', tab: 'security' } as const;
  return required
    ? {
        ...base,
        tone: 'ok',
        status: 'Key required',
        detail: 'Inference requests need a user’s API key.',
      }
    : {
        ...base,
        tone: 'attention',
        status: 'Open',
        detail: 'Anyone who can reach the gateway can run inference without an API key.',
      };
}

function alertsItem(entries: readonly ConfigEntry[]): OverviewItem | null {
  const enabled = find(entries, 'ALERTS_ENABLED');
  if (!enabled) return null;
  const base = { id: 'alerts', title: 'Alerts', tab: 'alerts', hash: 'config-alerts' } as const;
  if ((enabled.value ?? enabled.default) !== true) {
    return { ...base, tone: 'off', status: 'Off', detail: 'The gateway sends no alerts.' };
  }
  const webhooks = alertWebhookEntries(entries);
  const channel = webhooks ? savedAlertChannel(webhooks) : null;
  if (channel === 'none') {
    return {
      ...base,
      tone: 'attention',
      status: 'No channel',
      detail: 'Alerts are on, but no notification channel is chosen to deliver them.',
    };
  }
  return {
    ...base,
    tone: 'ok',
    status: 'On',
    detail:
      channel === 'slack'
        ? 'Alerts are sent to Slack.'
        : channel === 'wecom'
          ? 'Alerts are sent to WeCom.'
          : 'Alerts are on.',
  };
}

/**
 * The Overview's lines, in the order an administrator sets a gateway up:
 * where it is, how it mails, who may join, who may use it, who hears when it
 * breaks. `entries` is `null` while the configuration could not be read.
 */
export function overviewItems(
  entries: readonly ConfigEntry[] | null,
  runtime: readonly RuntimeSettingItem[],
): OverviewItem[] {
  const config = entries ?? [];
  return [
    siteItem(config),
    emailItem(config),
    signupItem(config, runtime),
    apiAccessItem(runtime),
    alertsItem(config),
  ].filter((item): item is OverviewItem => item !== null);
}
