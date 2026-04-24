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
});
