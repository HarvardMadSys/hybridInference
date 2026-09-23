'use client';

import type { ComponentType } from 'react';

import {
  AUTH_MESSAGE_KEYS,
  type AuthAppearance,
  type AuthFieldLayout,
  type AuthFrameProps,
  type AuthMessageKey,
  type AuthMessages,
  type ConsentItem,
  type ConsentItems,
  type LandingPageProps,
  type ModuleLegalText,
  type SiteUiClientModule,
  type SiteUiModuleDescriptor,
} from './contract';
import * as activeModule from '@site-ui/client';

/**
 * The compiled-in module, normalized once for the host. The descriptor is
 * required; Landing, AuthFrame, fieldLayout, authAppearance, authMessages and
 * the legal set — TermsFrame, TermsContent and consentItems — are optional
 * named exports. A default export is not part of the contract and is not read.
 */

/**
 * The module as the host reads it: every optional export resolved, once, to a
 * value or to "the host's own".
 *
 * The legal exports are one field because they are one decision — the
 * contract accepts all three or none — so a caller asks one question and cannot
 * find a frame without the text it frames.
 */
export interface ActiveSiteUi {
  readonly descriptor: SiteUiModuleDescriptor;
  readonly Landing: ComponentType<LandingPageProps> | null;
  readonly AuthFrame: ComponentType<AuthFrameProps> | null;
  /** The module's own legal text, or `null` when the console's is published. */
  readonly legalText: ModuleLegalText | null;
  readonly fieldLayout: AuthFieldLayout | undefined;
  readonly authAppearance: AuthAppearance | undefined;
  readonly authMessages: AuthMessages | undefined;
}

/**
 * The module's exports, checked against the client contract.
 *
 * An annotation, not a cast. `@site-ui/client` is whichever module the build
 * selected, so this line is where TypeScript compares that module's exports
 * with `SiteUiClientModule`: a `Landing` that is a number, a frame that takes
 * the wrong props or a descriptor missing a field fails `npm run type-check`
 * and `next build` here, naming the export. A cast through `unknown` compiled
 * all of those and left them to crash at runtime.
 */
const exported: SiteUiClientModule = activeModule;

/** The exports the host renders as components. */
type ComponentExport = 'Landing' | 'AuthFrame' | 'TermsFrame' | 'TermsContent' | 'fieldLayout';

/** The module's name in an error, so a failed build points at the right module. */
const MODULE_NAME = `The Site UI module '${String(exported.descriptor?.id)}'`;

/** React's markers for `memo`, `forwardRef` and `lazy` components, which are objects. */
const EXOTIC_COMPONENTS: ReadonlySet<unknown> = new Set([
  Symbol.for('react.memo'),
  Symbol.for('react.forward_ref'),
  Symbol.for('react.lazy'),
]);

function isComponent(value: unknown): boolean {
  if (typeof value === 'function') return true;
  return (
    typeof value === 'object' &&
    value !== null &&
    EXOTIC_COMPONENTS.has((value as { $$typeof?: unknown }).$$typeof)
  );
}

/**
 * One component export, or `null` when the module does not supply it.
 *
 * `null` is a meaningful answer, not a missing one: a module without a
 * `Landing` is saying "the host's page is right for this route", and the host
 * then keeps its own page and chrome.
 *
 * The type check above is what normally stops a wrong export, at build time.
 * This is the runtime half, for what the compiler cannot see — an `any`, a cast
 * inside the module — and it fails as the module loads, naming the module and
 * the export, rather than leaving React to fail later on whichever page first
 * renders it.
 */
function component<K extends ComponentExport>(key: K): NonNullable<SiteUiClientModule[K]> | null {
  const value = exported[key];
  if (value === undefined || value === null) return null;
  if (!isComponent(value)) {
    throw new Error(
      `${MODULE_NAME} exports ${key} as ${describe(value)}. ` +
        `${key} must be a React component, or null to keep the host's default.`,
    );
  }
  return value as NonNullable<SiteUiClientModule[K]>;
}

function describe(value: unknown): string {
  if (Array.isArray(value)) return 'an array';
  return typeof value === 'object' ? 'an object' : `a ${typeof value}`;
}

/** The legal set, in the order a message names them. */
const LEGAL_TEXT_EXPORTS = ['TermsFrame', 'TermsContent', 'consentItems'] as const;

/**
 * The module's legal text, or `null` when it publishes none and the console's
 * terms and confirmations stand.
 *
 * All three exports or none. The union type stops a partial set, and an empty
 * list, in the type check `next build` runs. This stops what the compiler
 * cannot see, when the module loads — the first request, since every route is
 * rendered on demand and the build renders none — and before any page renders,
 * because a partial set is not something a page can recover from: `/terms`
 * would publish one text while the sign-up step asked visitors to accept
 * another.
 */
function legalText(): ModuleLegalText | null {
  const supplied = {
    TermsFrame: component('TermsFrame'),
    TermsContent: component('TermsContent'),
    consentItems: exported.consentItems ?? null,
  };
  const present = LEGAL_TEXT_EXPORTS.filter((key) => supplied[key] !== null);
  if (present.length === 0) return null;

  const { TermsFrame, TermsContent, consentItems } = supplied;
  if (TermsFrame === null || TermsContent === null || consentItems === null) {
    const missing = LEGAL_TEXT_EXPORTS.filter((key) => supplied[key] === null);
    throw new Error(
      `${MODULE_NAME} exports ${list(present)} without ${list(missing)}. A module that ` +
        'publishes its own legal text exports TermsFrame, TermsContent and consentItems ' +
        'together, so that /terms and the sign-up consent step show the same text.',
    );
  }
  return { TermsFrame, TermsContent, consentItems: checkedConsentItems(consentItems) };
}

function list(names: readonly string[]): string {
  return names.length > 1
    ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
    : names[0];
}

/**
 * The confirmations, checked and copied.
 *
 * Checked because the sign-up request records acceptance once every listed
 * confirmation is checked: an empty list would record it for a visitor who
 * confirmed nothing, and two items with one id would share a checkbox. Copied,
 * with only the fields the contract declares, and frozen, so nothing that holds
 * the module can change what the step asks after it has loaded.
 */
function checkedConsentItems(value: unknown): ConsentItems {
  const problem = (detail: string) => new Error(`${MODULE_NAME} exports consentItems ${detail}.`);
  if (!Array.isArray(value)) {
    throw problem(`as ${describe(value)}; it must be an array of { id, label } confirmations`);
  }
  if (value.length === 0) {
    throw problem(
      'as an empty list. The sign-up step records acceptance of the terms once every ' +
        'confirmation is checked, so it needs at least one',
    );
  }

  const ids = new Set<string>();
  const items = value.map((item: unknown, index): ConsentItem => {
    const { id, label, description } = (item ?? {}) as Record<string, unknown>;
    if (typeof id !== 'string' || id.trim() === '') {
      throw problem(`with no id at position ${index}; each needs a non-empty string id`);
    }
    if (ids.has(id)) throw problem(`with the id '${id}' twice; ids must be unique`);
    ids.add(id);
    if (typeof label !== 'string' || label.trim() === '') {
      throw problem(`with no label for '${id}'; each needs the sentence the visitor confirms`);
    }
    if (description !== undefined && typeof description !== 'string') {
      throw problem(`with a description for '${id}' that is not a string`);
    }
    return Object.freeze(description === undefined ? { id, label } : { id, label, description });
  });
  const [first, ...rest] = items;
  return Object.freeze([first, ...rest]);
}

const DECLARED_MESSAGE_KEYS: ReadonlySet<string> = new Set(AUTH_MESSAGE_KEYS);

/**
 * The module's wording, cut down to the keys `AUTH_MESSAGE_KEYS` declares.
 *
 * The type limits a module to those keys only as far as TypeScript looks: a
 * dictionary with one declared key type-checks with any others beside it, and
 * the translator answers whatever slot it is asked. Console pages translate
 * through the same `t()`, so `chat.title` in a module's dictionary would reword
 * `/chat` — a page the contract keeps closed. Filtered here, once, every
 * consumer sees only declared keys, and only string values: the contract
 * localizes sentences, not markup.
 *
 * The result has no prototype, so a slot named like an `Object` method finds
 * nothing rather than a function.
 */
function declaredMessages(messages: AuthMessages | undefined): AuthMessages | undefined {
  if (!messages) return undefined;
  const declared: AuthMessages = Object.create(null);
  for (const [key, value] of Object.entries(messages)) {
    if (DECLARED_MESSAGE_KEYS.has(key) && typeof value === 'string') {
      declared[key as AuthMessageKey] = value;
    }
  }
  return declared;
}

export const SITE_UI_CLIENT: ActiveSiteUi = {
  descriptor: exported.descriptor,
  Landing: component('Landing'),
  // Missing page-owning components preserve the host's chrome.
  AuthFrame: component('AuthFrame'),
  legalText: legalText(),
  fieldLayout: component('fieldLayout') ?? undefined,
  authAppearance: exported.authAppearance,
  authMessages: declaredMessages(exported.authMessages),
};
