// The Email category as one card: which service sends the gateway's mail,
// that service's settings under friendly names, the sender, and a test.
//
// The backend owns the rules: which transports exist (`EMAIL_PROVIDER`'s
// choices, extensions included), which settings apply to each (`applies_when`)
// and which are required (`required`, `missing`). This module only names
// things and reads those flags.

import type { ConfigEntry } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';

import { appliesNow, currentInput, matchingChoice, type ConfigDrafts } from './configDrafts';

export const EMAIL_CATEGORY = 'email';
export const EMAIL_PROVIDER_KEY = 'EMAIL_PROVIDER';
/** The sender of every built-in transport, SMTP and Cloudflare alike, despite the names. */
export const SENDER_KEYS: readonly string[] = ['SMTP_FROM_EMAIL', 'SMTP_FROM_NAME'];
const SENDER_ADDRESS_KEY = 'SMTP_FROM_EMAIL';

export interface EmailProviderInfo {
  label: string;
  description: string;
}

/** The built-in transports; an extension's is named by its value. */
export const EMAIL_PROVIDERS: Readonly<Record<string, EmailProviderInfo>> = {
  smtp: {
    label: 'SMTP server',
    description:
      'Any mail server or relay that accepts SMTP: your mail provider, Amazon SES, Postmark and the like.',
  },
  cloudflare: {
    label: 'Cloudflare Email Sending',
    description:
      "Cloudflare's email API: no mail server to run. The sender's domain must be onboarded for Email Sending.",
  },
};

export function emailProviderLabel(name: string): string {
  return EMAIL_PROVIDERS[name]?.label ?? name;
}

/** What each built-in transport cannot send without, as the backend checks it. */
const TRANSPORT_KEYS: Readonly<Record<string, readonly string[]>> = {
  smtp: ['SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD'],
  cloudflare: ['CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_EMAIL_API_TOKEN'],
};

interface FieldText {
  label: string;
  /** Replaces the backend's description, which also says when the setting is required. */
  description: string | ((provider: string) => string);
}

/**
 * The built-in settings under the names an administrator looks for. The card
 * says once why they are required, so these leave that out. An extension's
 * settings keep their key and the backend's description.
 */
const FIELD_TEXT: Readonly<Record<string, FieldText>> = {
  SMTP_HOST: {
    label: 'SMTP host',
    description:
      "Your mail server's host name, such as smtp.example.com. Its TLS certificate must be valid for this name.",
  },
  SMTP_PORT: {
    label: 'Port',
    description: '587 for STARTTLS, which most servers use; 465 for TLS from the first byte.',
  },
  SMTP_USER: { label: 'Username', description: 'The user the gateway signs in to the server as.' },
  SMTP_PASSWORD: {
    label: 'Password',
    description: 'The password or app password for that user. A saved password is never shown.',
  },
  CLOUDFLARE_ACCOUNT_ID: {
    label: 'Account ID',
    description:
      "The 32-character ID in your Cloudflare dashboard's address: dash.cloudflare.com/<account ID>/…",
  },
  CLOUDFLARE_EMAIL_API_TOKEN: {
    label: 'API token',
    description:
      'An API token with the Email Sending: Edit permission on that account. A saved token is never shown.',
  },
  SMTP_FROM_EMAIL: {
    label: 'Sender address',
    description: (provider) =>
      provider === 'cloudflare'
        ? 'The address mail comes from. Its domain must be onboarded under Email Service → Email Sending in Cloudflare.'
        : 'The address mail comes from. Use one your mail server may send as.',
  },
  SMTP_FROM_NAME: {
    label: 'Sender name',
    description: 'The name recipients see beside the sender address.',
  },
};

/** The friendly label of a setting in the card; its key when it has none. */
export function emailFieldLabel(key: string): string {
  return FIELD_TEXT[key]?.label ?? key;
}

/** `entry` as the card shows it: the friendly description for `provider`, else its own. */
export function emailFieldEntry(entry: ConfigEntry, provider: string): ConfigEntry {
  const text = FIELD_TEXT[entry.key];
  if (!text) return entry;
  const description =
    typeof text.description === 'function' ? text.description(provider) : text.description;
  return { ...entry, description };
}

/** Whether a search for `query` should find the card through this setting's friendly text. */
export function emailFieldMatches(entry: ConfigEntry, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle || entry.category !== EMAIL_CATEGORY) return false;
  const text = FIELD_TEXT[entry.key];
  const words = [text?.label, ...Object.values(EMAIL_PROVIDERS).map((info) => info.label)];
  if (entry.key === EMAIL_PROVIDER_KEY) words.push('delivery service', 'email delivery');
  return words.some((word) => word?.toLowerCase().includes(needle));
}

/** The transport setting, when the backend offers a choice of transports. */
export function emailProviderEntry(entries: readonly ConfigEntry[]): ConfigEntry | null {
  const entry = entries.find(
    (candidate) => candidate.key === EMAIL_PROVIDER_KEY && candidate.category === EMAIL_CATEGORY,
  );
  return entry && (entry.choices?.length ?? 0) > 1 ? entry : null;
}

function asChoice(entry: ConfigEntry, raw: string): string {
  const value = raw || String(entry.default ?? '');
  return matchingChoice(entry, value) ?? value;
}

/** The transport the card shows now, an unsaved choice included. */
export function selectedEmailProvider(entry: ConfigEntry, drafts: ConfigDrafts): string {
  return asChoice(entry, String(currentInput(entry, drafts[entry.key])));
}

/** The transport the saved configuration sends through. */
export function savedEmailProvider(entry: ConfigEntry): string {
  return asChoice(entry, entry.value === null ? '' : String(entry.value));
}

export interface EmailDeliveryParts {
  provider: ConfigEntry;
  /** The selected transport's own settings, in the backend's order. */
  transport: ConfigEntry[];
  sender: ConfigEntry[];
}

/**
 * The card's settings with the values the editor shows now: the transport
 * choice, the chosen transport's settings, then the sender. A setting whose
 * stored value was refused shows whichever transport is chosen, as elsewhere,
 * so it can still be corrected or reset.
 */
export function emailDeliveryParts(
  entries: readonly ConfigEntry[],
  drafts: ConfigDrafts,
): EmailDeliveryParts | null {
  const provider = emailProviderEntry(entries);
  if (!provider) return null;
  const shown = entries.filter(
    (entry) =>
      entry.category === EMAIL_CATEGORY &&
      entry.key !== EMAIL_PROVIDER_KEY &&
      (appliesNow(entry, entries, drafts) || entry.invalid != null),
  );
  return {
    provider,
    transport: shown.filter((entry) => !SENDER_KEYS.includes(entry.key)),
    sender: shown.filter((entry) => SENDER_KEYS.includes(entry.key)),
  };
}

export type EmailDeliveryStatus =
  | { kind: 'ready'; provider: string }
  /** A built-in transport without everything it needs: `missing` are their labels. */
  | { kind: 'incomplete'; provider: string; missing: string[] }
  /** An extension's transport: the console cannot tell whether it is complete. */
  | { kind: 'unknown'; provider: string };

/** Whether the saved configuration can send, as far as the console can tell. */
export function savedEmailStatus(entries: readonly ConfigEntry[]): EmailDeliveryStatus | null {
  const entry = emailProviderEntry(entries);
  if (!entry) return null;
  const provider = savedEmailProvider(entry);
  const needed = TRANSPORT_KEYS[provider];
  if (!needed) {
    const missing = entries.filter(
      (candidate) =>
        candidate.missing && candidate.applies_when?.values.includes(provider) === true,
    );
    return missing.length > 0
      ? { kind: 'incomplete', provider, missing: missing.map((item) => item.key) }
      : { kind: 'unknown', provider };
  }
  const missing = needed.filter(
    (key) => !entries.find((candidate) => candidate.key === key)?.is_set,
  );
  return missing.length > 0
    ? { kind: 'incomplete', provider, missing: missing.map(emailFieldLabel) }
    : { kind: 'ready', provider };
}

/** Whether public sign-up's email verification is what makes the card's settings required. */
export function emailRequired(entries: readonly ConfigEntry[]): boolean {
  return entries.some(
    (entry) =>
      entry.category === EMAIL_CATEGORY && entry.key !== EMAIL_PROVIDER_KEY && entry.required,
  );
}

/**
 * A warning about the sender address shown now, when mail from it would be
 * refused: the built-in default, or any address at `localhost`.
 */
export function senderWarning(
  entries: readonly ConfigEntry[],
  drafts: ConfigDrafts,
  provider: string,
): string | null {
  const entry = entries.find((candidate) => candidate.key === SENDER_ADDRESS_KEY);
  if (!entry || !(provider in EMAIL_PROVIDERS)) return null;
  const address = String(currentInput(entry, drafts[entry.key]) || entry.default || '').trim();
  if (!/@localhost$/i.test(address)) return null;
  return provider === 'cloudflare'
    ? `Cloudflare will not send from ${address}. Use an address on a domain onboarded under Email Service → Email Sending.`
    : `Most mail servers refuse mail from ${address}. Use an address on your own domain.`;
}

/** A plain check before the backend's: something@something.tld, nothing more. */
export function looksLikeEmailAddress(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

export type EmailTestResult =
  | { sent: true; to: string; provider: string }
  | { sent: false; message: string };

/**
 * What the card says when a test was not sent. The backend's sentences say
 * what to fix (the settings still needed, a rejected password, a token
 * without permission) and carry no credential, so they are shown as they are.
 */
export function emailTestFailureMessage(error: unknown): string {
  const generic = 'The test email could not be sent. Try again in a moment.';
  if (!(error instanceof APIError)) return generic;
  if (error.statusCode === 404) {
    return 'This backend cannot send test emails. Upgrade it and try again.';
  }
  if (error.statusCode === 429) return 'Wait a few seconds, then send another test.';
  // A response without a sentence of the backend's, such as an edge proxy's
  // error page, reads "HTTP 502: Bad Gateway".
  if (!error.message || error.message.startsWith('HTTP ')) return generic;
  return error.message;
}
