export type ValidatedNumeric = { ok: true; value: number } | { ok: false; error: string };

export function validateNumericSettingInput(
  raw: string,
  opts: { min?: number | null; max?: number | null; integer: boolean },
): ValidatedNumeric {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return { ok: false, error: 'Enter a value.' };
  const n = Number(trimmed);
  if (!Number.isFinite(n)) return { ok: false, error: 'Not a number.' };
  if (opts.integer && !Number.isInteger(n)) {
    return { ok: false, error: 'Must be a whole number.' };
  }
  if (opts.min != null && n < opts.min) {
    return { ok: false, error: `Must be ≥ ${opts.min}.` };
  }
  if (opts.max != null && n > opts.max) {
    return { ok: false, error: `Must be ≤ ${opts.max}.` };
  }
  return { ok: true, value: n };
}
