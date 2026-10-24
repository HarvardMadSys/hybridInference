/**
 * Resolve one console interface string, without React.
 *
 * Deliberately *not* a `'use client'` module. `useT` lives next to the provider
 * it reads, which makes that file a client boundary; a server page that called a
 * helper exported from it was calling a client function from the server, and
 * Next refuses that at runtime with "Attempted to call translate() from the
 * server but translate is on the client".
 *
 * The failure is worth naming because of how it presented: `/terms` and `/team`
 * render `generateMetadata` on the server, the call threw there, and the page
 * fell into the error boundary and rendered "Something went wrong" — with the
 * *route* still answering 200, so a status-code check saw nothing wrong. A
 * screenshot did.
 *
 * The console's strings are the English literals at each call site; another
 * language is a catalog of translations keyed by slot (`messages/<locale>.json`).
 * The plain resolver below returns the fallback it is given, which is the
 * console in English.
 */

export type Translate = (slot: string, fallback: string) => string;

/** The console's translator: every string is the one the caller passes. */
export const translate: Translate = (_slot, fallback) => fallback;

/**
 * One translated string, with the English it translates.
 *
 * The English is stored beside the translation so that editing the source
 * retires the translation instead of leaving it to say something the page no
 * longer does — the same rule the docs' gettext catalogs follow, where a
 * changed paragraph's entry turns `fuzzy` and the build stops using it.
 */
export interface CatalogEntry {
  source: string;
  text: string;
}

/**
 * A language's translations, by slot.
 *
 * A slot is usually one sentence, but a few say different English in different
 * places — a module may word the slot once for all of them, which is why they
 * share it — so a slot can hold one entry per English source.
 */
export type Catalog = Readonly<Record<string, CatalogEntry | readonly CatalogEntry[]>>;

/** The translation of `source` in a slot's entries, or `undefined`. */
function entryText(
  entries: CatalogEntry | readonly CatalogEntry[] | undefined,
  source: string,
): string | undefined {
  if (!entries) return undefined;
  const list: readonly CatalogEntry[] = Array.isArray(entries)
    ? entries
    : [entries as CatalogEntry];
  return list.find((entry) => entry.source === source)?.text;
}

/**
 * Build a translator over a language's catalog and, ahead of it, a module's
 * wording for that language.
 *
 * A catalog entry is used only while its `source` is exactly the English the
 * call site passes; once the English changes, the slot falls back to the new
 * English until someone translates it again. An override is the module's own
 * sentence and is not checked against anything — the module owns it.
 */
export function catalogTranslator(
  catalog: Catalog | undefined,
  overrides?: Readonly<Record<string, string>>,
): Translate {
  if (!catalog && !overrides) return translate;
  return (slot, fallback) => {
    const override = overrides?.[slot];
    if (override !== undefined) return override;
    return entryText(catalog?.[slot], fallback) ?? fallback;
  };
}
