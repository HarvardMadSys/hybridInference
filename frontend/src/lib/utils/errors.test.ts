import { describe, expect, it } from 'vitest';

import { APIError, ERROR_MESSAGES, getErrorMessage } from './errors';

describe('getErrorMessage', () => {
  it('maps APIError codes to product copy', () => {
    expect(getErrorMessage(new APIError('EMAIL_NOT_VERIFIED', 'raw backend message', 403))).toBe(
      ERROR_MESSAGES.EMAIL_NOT_VERIFIED,
    );
  });

  it('falls back to the provided Error message', () => {
    expect(getErrorMessage(new Error('Backend unavailable'))).toBe('Backend unavailable');
  });

  it('returns a stable default for unknown thrown values', () => {
    expect(getErrorMessage({ unexpected: true })).toBe(ERROR_MESSAGES.UNKNOWN_ERROR);
  });

  it('returns the backend detail when APIError code is the default UNKNOWN_ERROR sentinel', () => {
    expect(
      getErrorMessage(
        new APIError('UNKNOWN_ERROR', 'subject and body_html are required', 422),
      ),
    ).toBe('subject and body_html are required');
  });

  it('returns the curated UNKNOWN_ERROR copy when APIError has no message', () => {
    expect(getErrorMessage(new APIError('UNKNOWN_ERROR', '', 500))).toBe(
      ERROR_MESSAGES.UNKNOWN_ERROR,
    );
  });

  it('returns the backend message when a plain Error has UNKNOWN_ERROR code attached', () => {
    const err = new Error('boom from backend') as Error & { code?: string };
    err.code = 'UNKNOWN_ERROR';
    expect(getErrorMessage(err)).toBe('boom from backend');
  });
});
