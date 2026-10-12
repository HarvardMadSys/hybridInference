// Error bodies for the setup and configuration endpoints, read literally.
//
// `jsonOrThrow` classifies a failure by matching words in its message, which
// suits the login and reset flows it was written for and misreads these
// endpoints: a configuration error such as "SLACK_BOT_TOKEN: invalid value"
// contains "invalid" and "token", so it would come back as INVALID_TOKEN and
// be shown as "Invalid or expired token". Here the message is the server's
// `detail`, verbatim, and the status code carries the meaning.

import { APIError, httpStatusToErrorCode } from '@/lib/utils/errors';

/** One FastAPI/pydantic validation error. */
interface ValidationItem {
  loc?: unknown;
  msg?: unknown;
}

export interface ApiErrorDetail {
  /** A sentence to show; empty when the body had none. */
  message: string;
  /** Validation messages by request field, from a 422 `detail` list. */
  fields: Record<string, string>;
}

function cleanValidationMessage(message: string): string {
  // Pydantic prefixes a validator's own message with its error type.
  return message.replace(/^(Value error|Assertion failed),\s*/i, '');
}

/** Read the `detail` of an error body: a string, or a list of field errors. */
export function describeErrorBody(body: unknown): ApiErrorDetail {
  const fields: Record<string, string> = {};
  if (!body || typeof body !== 'object') return { message: '', fields };

  const record = body as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === 'string') return { message: detail, fields };

  if (Array.isArray(detail)) {
    const messages: string[] = [];
    for (const item of detail as ValidationItem[]) {
      if (!item || typeof item.msg !== 'string') continue;
      const message = cleanValidationMessage(item.msg);
      const loc = Array.isArray(item.loc) ? item.loc : [];
      const field = [...loc].reverse().find((part) => typeof part === 'string' && part !== 'body');
      if (typeof field === 'string' && !(field in fields)) fields[field] = message;
      messages.push(typeof field === 'string' ? `${field}: ${message}` : message);
    }
    return { message: messages.join('; '), fields };
  }

  // The gateway's typed errors: { error: { message, type, code } }.
  const error = record.error;
  if (error && typeof error === 'object') {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === 'string') return { message, fields };
  }
  return { message: '', fields };
}

/**
 * Build the error for a failed response without second-guessing its message.
 *
 * The code is a plain function of the status, so `getErrorMessage` shows the
 * server's own sentence for every 4xx that has one (none of these codes are in
 * its table) and the curated text for a rate limit or an edge proxy's HTML
 * error page.
 */
export async function apiErrorFromResponse(resp: Response): Promise<APIError> {
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    return new APIError(
      httpStatusToErrorCode(resp.status),
      `HTTP ${resp.status}: ${resp.statusText}`,
      resp.status,
    );
  }

  const { message, fields } = describeErrorBody(body);
  if (resp.status === 429) {
    return new APIError('RATE_LIMIT_EXCEEDED', message, resp.status);
  }
  if (!message) {
    return new APIError(
      httpStatusToErrorCode(resp.status),
      `HTTP ${resp.status}: ${resp.statusText}`,
      resp.status,
    );
  }
  const code = resp.status === 422 ? 'VALIDATION_ERROR' : `HTTP_${resp.status}`;
  return new APIError(
    code,
    message,
    resp.status,
    Object.keys(fields).length > 0 ? { fields } : undefined,
  );
}

/** `resp.json()` for a success, `apiErrorFromResponse` otherwise. */
export async function readJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw await apiErrorFromResponse(resp);
  return resp.json() as Promise<T>;
}
