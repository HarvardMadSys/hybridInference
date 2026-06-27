import { describe, expect, it } from 'vitest';

import { APIError, ERROR_MESSAGES, getErrorMessage, httpStatusToErrorCode } from './errors';

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
      getErrorMessage(new APIError('UNKNOWN_ERROR', 'subject and body_html are required', 422)),
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

describe('httpStatusToErrorCode', () => {
  it('maps gateway-timeout statuses to TIMEOUT_ERROR', () => {
    for (const status of [408, 504, 522, 524, 598]) {
      expect(httpStatusToErrorCode(status)).toBe('TIMEOUT_ERROR');
    }
  });

  it('maps bad-gateway / unavailable statuses to SERVICE_UNAVAILABLE', () => {
    for (const status of [502, 503, 521, 523]) {
      expect(httpStatusToErrorCode(status)).toBe('SERVICE_UNAVAILABLE');
    }
  });

  it('maps other 5xx statuses to SERVER_ERROR', () => {
    for (const status of [500, 520, 525]) {
      expect(httpStatusToErrorCode(status)).toBe('SERVER_ERROR');
    }
  });

  it('never returns NETWORK_ERROR (a response was received)', () => {
    for (const status of [400, 404, 413, 500, 502, 504]) {
      expect(httpStatusToErrorCode(status)).not.toBe('NETWORK_ERROR');
    }
  });

  it('falls back to UNKNOWN_ERROR for non-JSON 4xx pages', () => {
    expect(httpStatusToErrorCode(413)).toBe('UNKNOWN_ERROR');
  });
});
