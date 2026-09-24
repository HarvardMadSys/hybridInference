/**
 * The neutral module's account-page body, importable by the host.
 *
 * Split from `client.tsx` for one reason: `client.tsx` is the module entry, and
 * a file reached through the generated `@site-ui` stub cannot be imported by the
 * host directly without making the host depend on which module is installed.
 * The default card is not module-specific — it is the console's own look, and
 * the host renders it when no module frame claims the page — so it lives where
 * both can reach it and is defined once.
 *
 * The module entry re-exports nothing from here on purpose: a distribution's
 * `client.tsx` should not be able to satisfy the interface by borrowing the
 * neutral body, and the build would not notice if it did.
 */
export { NeutralAuthCard } from './client';
