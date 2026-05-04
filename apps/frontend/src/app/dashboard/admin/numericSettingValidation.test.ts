import { describe, expect, it } from 'vitest';

import { validateNumericSettingInput } from './numericSettingValidation';

describe('validateNumericSettingInput', () => {
  it('parses a valid integer', () => {
    const r = validateNumericSettingInput('5', { min: 1, max: 100, integer: true });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value).toBe(5);
  });

  it('rejects empty input', () => {
    const r = validateNumericSettingInput('', { integer: true });
    expect(r.ok).toBe(false);
  });

  it('rejects non-numeric input', () => {
    const r = validateNumericSettingInput('abc', { integer: true });
    expect(r.ok).toBe(false);
  });

  it('rejects fractional input when integer required', () => {
    const r = validateNumericSettingInput('1.5', { integer: true });
    expect(r.ok).toBe(false);
  });

  it('accepts fractional input when integer not required', () => {
    const r = validateNumericSettingInput('1.5', { integer: false });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value).toBe(1.5);
  });

  it('rejects values below min', () => {
    const r = validateNumericSettingInput('0', { min: 1, integer: true });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('1');
  });

  it('rejects values above max', () => {
    const r = validateNumericSettingInput('11', { max: 10, integer: true });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain('10');
  });

  it('accepts boundary values', () => {
    expect(validateNumericSettingInput('1', { min: 1, max: 10, integer: true }).ok).toBe(true);
    expect(validateNumericSettingInput('10', { min: 1, max: 10, integer: true }).ok).toBe(true);
  });

  it('treats null bounds as unbounded', () => {
    const r = validateNumericSettingInput('-5', { min: null, max: null, integer: true });
    expect(r.ok).toBe(true);
  });
});
