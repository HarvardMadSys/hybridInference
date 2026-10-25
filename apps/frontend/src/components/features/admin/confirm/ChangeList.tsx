/**
 * What a reviewed edit changes, for the confirmation dialog: one row per
 * changed field, `before → after`.
 *
 * Callers pass display strings. A secret's row says only what happens to it
 * ("Set → Replace", "Clear"), never its value. Long values (URLs, lists) wrap
 * anywhere so they never widen the dialog.
 */
export interface Change {
  label: string;
  before?: string;
  after: string;
}

export function ChangeList({ changes }: { changes: readonly Change[] }) {
  return (
    <ul className="space-y-1.5">
      {changes.map((change) => (
        <li key={change.label} className="[overflow-wrap:anywhere]">
          <span className="font-medium text-gray-900">{change.label}</span>
          <span className="block font-mono text-[12px]">
            {change.before === undefined ? change.after : `${change.before} → ${change.after}`}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** How an empty value reads in a change row. */
export function shown(value: string | number | boolean | null | undefined, empty = '(empty)') {
  if (value === null || value === undefined || value === '') return empty;
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  return String(value);
}

/** Changes between two flat records, for the fields listed, in that order. */
export function diffFields<T extends Record<string, unknown>>(
  before: T,
  after: T,
  fields: ReadonlyArray<{
    key: keyof T;
    label: string;
    format?: (value: T[keyof T]) => string;
  }>,
): Change[] {
  const changes: Change[] = [];
  for (const field of fields) {
    const a = before[field.key];
    const b = after[field.key];
    if (JSON.stringify(a) === JSON.stringify(b)) continue;
    const format = field.format ?? ((value: T[keyof T]) => shown(value as never));
    changes.push({ label: field.label, before: format(a), after: format(b) });
  }
  return changes;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
