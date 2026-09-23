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
 * The console has one language, English, and its strings are the literals at
 * each call site, so the plain resolver returns the fallback it is given. A
 * migration that needs a translated server-rendered string can extend this with
 * its own dictionary; nothing does today.
 */

export type Translate = (slot: string, fallback: string) => string;

/** The console's translator: every string is the one the caller passes. */
export const translate: Translate = (_slot, fallback) => fallback;

/**
 * Build a translator over a dictionary, falling back to the caller's literal.
 *
 * It answers any slot in the dictionary it is given, so the dictionary decides
 * which slots can change. `useT` passes the compiled-in module's
 * `authMessages`, which `module.tsx` has already cut down to the contract's
 * declared keys; anything else that holds a dictionary and is not a component
 * can use it the same way.
 */
export function translator(messages: Record<string, string> | undefined): Translate {
  if (!messages) return translate;
  return (slot, fallback) => messages[slot] ?? fallback;
}
