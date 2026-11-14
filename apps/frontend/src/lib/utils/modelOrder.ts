// Every list of models shown to a user is in alphabetical order of model id,
// whatever order the gateway returns them in (its registry order, which only
// the operator's configuration decides). Case is ignored and runs of digits
// compare as numbers, so `qwen3-8b` comes before `qwen3-32b`. The locale is
// fixed so the order does not change with the reader's language.
const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

export function compareModelIds(a: string, b: string): number {
  // Ids equal but for case still get one fixed order.
  return collator.compare(a, b) || (a < b ? -1 : a > b ? 1 : 0);
}

/** A copy of `models` in alphabetical order of id. */
export function sortModelsById<T extends { id: string }>(models: readonly T[]): T[] {
  return [...models].sort((a, b) => compareModelIds(a.id, b.id));
}
