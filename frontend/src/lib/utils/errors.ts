export const ERROR_MESSAGES: Record<string, string> = {
  // Authentication errors
  USER_ALREADY_EXISTS: 'This email is already registered',
  WEAK_PASSWORD: 'Password must be at least 8 characters with uppercase, lowercase, and numbers',
  INVALID_CREDENTIALS: 'Invalid email or password',
  EMAIL_NOT_VERIFIED: 'Please verify your email first',
  ACCOUNT_SUSPENDED: 'Account has been suspended. Please contact support',
  TOKEN_EXPIRED: 'Session has expired. Please login again',
  INVALID_TOKEN: 'Invalid or expired token',
  TOKEN_ALREADY_USED: 'This verification link has already been used',
  SESSION_NOT_FOUND: 'Session not found. Please login again',
  SESSION_REVOKED: 'Session has been revoked',

  // API Key errors
  DUPLICATE_API_KEY: 'You already have an active API key',
  API_KEY_NOT_FOUND: 'API key not found',

  // Quota errors
  QUOTA_EXCEEDED: 'Daily usage quota exceeded',

  // Rate limiting
  RATE_LIMIT_EXCEEDED: 'Too many requests. Please try again later',

  // Network errors
  NETWORK_ERROR: 'Network error. Please check your connection',
  TIMEOUT_ERROR: 'Request timeout. Please try again',

  // Default
  UNKNOWN_ERROR: 'An unknown error occurred. Please try again',
};

export function getErrorMessage(error: unknown): string {
  if (error instanceof APIError) {
    if (error.code && ERROR_MESSAGES[error.code]) {
      return ERROR_MESSAGES[error.code];
    }
    return error.message || ERROR_MESSAGES.UNKNOWN_ERROR;
  }

  if (error instanceof Error) {
    const errorWithCode = error as Error & { code?: string };
    if (errorWithCode.code && ERROR_MESSAGES[errorWithCode.code]) {
      return ERROR_MESSAGES[errorWithCode.code];
    }
    return error.message || ERROR_MESSAGES.UNKNOWN_ERROR;
  }

  return ERROR_MESSAGES.UNKNOWN_ERROR;
}

export class APIError extends Error {
  constructor(
    public code: string,
    message: string,
    public statusCode?: number,
    public details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'APIError';
  }
}
