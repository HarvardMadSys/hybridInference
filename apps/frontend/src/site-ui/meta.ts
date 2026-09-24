import type { Metadata } from 'next';

import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { pageTitle } from '@/config/site-metadata';
import { fill } from '@/lib/utils/interpolate';
import { META_MESSAGE_KEYS, type MetaMessageKey, type MetaMessages } from '@/site-ui/contract';
import { metaMessages } from '@site-ui/server';

/**
 * Titles and descriptions of the public routes, in the compiled-in module's
 * wording when it has some.
 *
 * Server-side, because metadata is: `generateMetadata` runs in a server
 * component, which reads no client dictionary, so the wording comes from the
 * module's server entry through the generated bridge. Only the public routes
 * read it — the console's pages keep their English titles whatever the
 * module's dictionary holds.
 */

/** A public route with a title of its own, named as its keys are. */
export type MetaPage = 'home' | 'login' | 'signup' | 'forgot' | 'reset' | 'verify' | 'terms';

const DECLARED_META_KEYS: ReadonlySet<string> = new Set(META_MESSAGE_KEYS);

/**
 * The module's page wording, cut down to the keys `META_MESSAGE_KEYS` declares
 * and to string values — as `module.tsx` does for the client dictionary, and
 * for the same reason: a dictionary with one declared key type-checks with any
 * others beside it. No prototype, so a key named like an `Object` method finds
 * nothing.
 */
export function declaredMetaMessages(messages: unknown): MetaMessages {
  const declared: MetaMessages = Object.create(null);
  if (typeof messages !== 'object' || messages === null) return declared;
  for (const [key, value] of Object.entries(messages)) {
    if (DECLARED_META_KEYS.has(key) && typeof value === 'string') {
      declared[key as MetaMessageKey] = value;
    }
  }
  return declared;
}

const MODULE_WORDING = declaredMetaMessages(metaMessages);

/**
 * The console's own wording, where it has any. `/terms` has always had its own
 * title; the other public routes use the site's title and description, which
 * the root layout sets.
 */
const CONSOLE_WORDING: Partial<Record<MetaPage, { title: string; description: string }>> = {
  terms: { title: 'Terms of Service', description: 'Terms of Service for {app_name}.' },
};

/**
 * A public page's `generateMetadata`.
 *
 * Returns only what there is wording for, so a page the module does not word —
 * and every page, on a build without a module — keeps what it had: the site's
 * title and description from the root layout, or the console's terms title.
 */
export async function publicPageMetadata(page: MetaPage): Promise<Metadata> {
  const siteConfig = await loadRuntimeSiteConfig();
  const values = { app_name: siteConfig.branding.appName };
  const title = MODULE_WORDING[`meta.${page}.title`] ?? CONSOLE_WORDING[page]?.title;
  const description =
    MODULE_WORDING[`meta.${page}.description`] ?? CONSOLE_WORDING[page]?.description;

  const metadata: Metadata = {};
  if (title !== undefined) {
    // The home page's title is the whole title, as the site's name alone is.
    const text = fill(title, values);
    metadata.title = page === 'home' ? text : pageTitle(siteConfig, text);
  }
  if (description !== undefined) metadata.description = fill(description, values);
  return metadata;
}
