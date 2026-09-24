/**
 * Substitute `{name}` placeholders in a resolved string.
 *
 * Whole sentences are translated rather than fragments, so a language whose
 * word order differs can move the values; this is the piece that moves them.
 * An unknown placeholder is left standing, which makes a mistranslation show up
 * in review instead of silently rendering an empty gap.
 */
export function fill(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => values[name] ?? placeholder);
}
