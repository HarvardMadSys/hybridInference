// Absolute imports of the sibling contract, not the `@/` alias: this file is
// reached through the generated `@site-ui/server` bridge, and an aliased import
// inside a module reached by an alias is one more resolution rule for the
// bundler, `tsc` and Vitest to disagree about.
import type { SiteUiMetadata } from '../contract';

/**
 * The neutral module's server half.
 *
 * Read by the root layout, which runs on the server and must never touch the
 * client half: `client.tsx` is a component tree, and importing it into server
 * code would drag hooks and `usePathname` into a module that has no request.
 * Everything here is a plain serializable value.
 *
 * `metadata` is deliberately absent. The console's title, description and icons
 * come from `/site-config` and `rootMetadata()`, exactly as before.
 */

export const metadata: SiteUiMetadata | undefined = undefined;

/**
 * The document language for the public routes.
 *
 * Empty means "this UI declares no fixed language", and the console's own
 * language wins. The neutral UI is the console, so it declares nothing.
 */
export const locale = '';
