'use client';

import {
  AUTH_MESSAGE_KEYS,
  type AuthMessageKey,
  type AuthMessages,
  type SiteUiClientModule,
} from './contract';
import * as activeModule from '@site-ui/client';

/**
 * The compiled-in module, normalized once for the host. The descriptor is
 * required; Landing, AuthFrame, TermsFrame, fieldLayout, authAppearance and
 * authMessages are optional named exports. A default export is not part of the
 * contract and is not read.
 */

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
type ComponentExport = 'Landing' | 'AuthFrame' | 'TermsFrame' | 'fieldLayout';

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
    const found = typeof value === 'object' ? 'an object' : `a ${typeof value}`;
    throw new Error(
      `The Site UI module '${String(exported.descriptor?.id)}' exports ${key} as ${found}. ` +
        `${key} must be a React component, or null to keep the host's default.`,
    );
  }
  return value as NonNullable<SiteUiClientModule[K]>;
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

export const SITE_UI_CLIENT: SiteUiClientModule = {
  descriptor: exported.descriptor,
  Landing: component('Landing'),
  // Missing page-owning components preserve the host's chrome.
  AuthFrame: component('AuthFrame'),
  TermsFrame: component('TermsFrame'),
  fieldLayout: component('fieldLayout') ?? undefined,
  authAppearance: exported.authAppearance,
  authMessages: declaredMessages(exported.authMessages),
};
