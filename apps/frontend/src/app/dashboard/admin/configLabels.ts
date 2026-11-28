/**
 * Plain names for the built-in configuration settings, as Admin → Settings
 * shows them: "Administrator emails" rather than `ADMIN_EMAILS`. The variable's
 * name is still shown beside each, for the administrator who sets it in `.env`
 * or reads it in the docs, and a search finds a setting by either.
 *
 * Only a built-in setting gets one. A variable the routes reference, a
 * numbered provider key and one an administrator added are known by their
 * name, which is what a route's `${NAME}` says, and a setting this list does
 * not name yet — one a later backend adds — keeps its name too.
 */

import type { ConfigEntry } from '@/lib/api/config';
import {
  EMAIL_CATEGORY,
  EMAIL_PROVIDER_KEY,
  emailFieldLabel,
} from '@/components/features/configuration/emailDeliveryDrafts';

const LABELS: Readonly<Record<string, string>> = {
  // --- application documents ---
  MODEL_CATALOG: 'Model catalog',
  ROUTING_POLICY: 'Routing policy',
  ALERT_RULES: 'Alert rules',
  SITE_CONFIGURATION: 'Site configuration',
  SITE_BRANDING: 'Branding',
  SSO_PROVIDERS: 'Single sign-on providers',

  // --- general ---
  BASE_URL: 'Public API address',
  FRONTEND_URL: 'Console address',
  SITE_NAME: 'Site name',
  SITE_PUBLIC_BASE_URL: 'Site address in backend content',
  SITE_SUPPORT_EMAIL: 'Support email',
  SITE_DOCS_URL: 'Documentation address',
  LOG_LEVEL: 'Log level',
  LOG_FORMAT: 'Log format',

  // --- security ---
  JWT_SECRET_KEY: 'Session signing secret',
  API_KEY_SECRET: 'API key secret',
  ADMIN_TOKEN: 'Admin API token',
  ADMIN_EMAILS: 'Administrator emails',
  JWT_ALGORITHM: 'Session signing algorithm',
  JWT_ACCESS_TOKEN_EXPIRE_MINUTES: 'Access token lifetime (minutes)',
  JWT_REFRESH_TOKEN_EXPIRE_DAYS: 'Refresh token lifetime (days)',
  COOKIE_SECURE: 'Secure session cookie',
  COOKIE_DOMAIN: 'Session cookie domain',
  COOKIE_SAMESITE: 'Session cookie SameSite',
  LOGIN_RATE_LIMIT_PER_15MIN: 'Sign-in attempts per account from one address (15 min)',
  LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN: 'Sign-in attempts per account (15 min)',
  LOGIN_RATE_LIMIT_PER_HOUR_PER_IP: 'Sign-in attempts per address (hour)',
  AUTH_FAILURE_BLOCK_ENABLED: 'Block failing clients',
  AUTH_FAILURE_BLOCK_THRESHOLD: 'Failures before a block',
  AUTH_FAILURE_BLOCK_WINDOW_SEC: 'Failure window (seconds)',
  AUTH_FAILURE_BLOCK_DURATION_SEC: 'Block duration (seconds)',
  UNRESOLVED_AUTH_FAILURE_BLOCK_THRESHOLD: 'Failures before a block, unknown address',
  UNRESOLVED_AUTH_FAILURE_BLOCK_WINDOW_SEC: 'Failure window, unknown address (seconds)',
  UNRESOLVED_AUTH_FAILURE_BLOCK_DURATION_SEC: 'Block duration, unknown address (seconds)',
  AUTH_FAILURE_BLOCK_EXEMPT_IPS: 'Addresses never blocked',
  AUTH_FAILURE_IDENTIFY_CALLER: 'Name the account of a rejected key',

  // --- sign-up ---
  SIGNUP_NOTIFY_EMAILS: 'New-account notification emails',
  SIGNUP_DEFAULT_DAILY_QUOTA_USD: 'Default daily quota (deprecated)',
  SIGNUP_RATE_LIMIT_PER_HOUR: 'Sign-ups per address (hour)',
  SIGNUP_RATE_LIMIT_PER_DAY: 'Sign-ups per address (day)',
  SIGNUP_REQUIRE_RESOLVED_CLIENT_IP: 'Refuse sign-ups from an unknown address',
  UNRESOLVED_SIGNUP_RATE_LIMIT_PER_HOUR: 'Sign-ups from unknown addresses (hour)',
  UNRESOLVED_SIGNUP_RATE_LIMIT_PER_DAY: 'Sign-ups from unknown addresses (day)',
  TURNSTILE_SECRET_KEY: 'Captcha secret (Cloudflare Turnstile)',

  // --- phone authentication ---
  SMS_AUTH_ENABLED: 'Phone sign-in',
  SMS_PROVIDER: 'SMS provider',
  SMS_DEFAULT_COUNTRY_CODE: 'Default country code',
  SMS_ALLOWED_PREFIXES: 'Allowed number prefixes',
  SMS_OTP_EXPIRE_SECONDS: 'Code lifetime (seconds)',
  SMS_OTP_SEND_COOLDOWN_SECONDS: 'Wait between codes (seconds)',
  SMS_OTP_SEND_PER_PHONE_HOUR: 'Codes per number (hour)',
  SMS_OTP_SEND_PER_PHONE_DAY: 'Codes per number (day)',
  SMS_OTP_SEND_PER_IP_HOUR: 'Codes per address (hour)',
  SMS_OTP_SEND_PER_IP_DAY: 'Codes per address (day)',
  SMS_OTP_SEND_UNRESOLVED_PER_HOUR: 'Codes to unknown addresses (hour)',
  SMS_OTP_SEND_UNRESOLVED_PER_DAY: 'Codes to unknown addresses (day)',
  SMS_OTP_MAX_ATTEMPTS: 'Attempts per code',
  SMS_OTP_VERIFY_PER_PHONE_15MIN: 'Code checks per number (15 min)',
  SMS_OTP_VERIFY_PER_IP_HOUR: 'Code checks per address (hour)',

  // --- providers ---
  PROVIDER_ROUTE_TYPES: 'Route types per provider',
  LOCAL_API_KEY: 'Local proxies’ shared key',
  MODEL_QUEUE_STATUS_URL: 'Model queue status address',
  MODEL_QUEUE_STATUS_API_KEY: 'Model queue status key',

  // --- routing ---
  ENABLE_ROUTEWISE: 'RouteWise as the default router',
  UPSTREAM_CONCURRENCY_ENABLED: 'Limit outbound concurrency',
  UPSTREAM_CONCURRENCY_INITIAL_LIMIT: 'Starting outbound concurrency',
  UPSTREAM_CONCURRENCY_MAX_LIMIT: 'Highest outbound concurrency',
  UPSTREAM_CONCURRENCY_PROBE_SUCCESS_INTERVAL: 'Successes between upward probes',
  UPSTREAM_CONCURRENCY_ACQUIRE_TIMEOUT_SEC: 'Wait for an outbound slot (seconds)',
  ROUTER_HEALTH_EWMA_ALPHA: 'Availability smoothing',
  CIRCUIT_FAILURE_THRESHOLD: 'Failures that open a circuit',
  CIRCUIT_COOLDOWN_SECONDS: 'Circuit cooldown (seconds)',
  CIRCUIT_MIN_AVAILABILITY: 'Availability that opens a circuit',
  CIRCUIT_CALLER_WINDOW_SECONDS: 'One caller’s repeat-failure window (seconds)',
  ROUTING_AFFINITY_ENABLED: 'Keep callers on their cached endpoint',
  ROUTING_AFFINITY_MAX_AGE_SEC: 'Longest stay on one endpoint (seconds)',
  ROUTING_PREFILL_AWARE_ENABLED: 'Steer around large prefills',
  ROUTING_PREFILL_ELEPHANT_TOKENS: 'Very large prefill (tokens)',
  ROUTING_PREFILL_ELEPHANT_LIMIT: 'Very large prefills per endpoint',
  ROUTING_PREFILL_INTERVENE_TOKENS: 'Backlog that overrides weights (tokens)',
  ROUTING_PREFILL_AFFINITY_CEILING: 'Backlog that ignores a caller’s endpoint (tokens)',
  ROUTING_PREFILL_HINT_TTL_SEC: 'Prompt size memory (seconds)',
  ROUTING_PRIORITY_INTERACTIVE: 'Priority of ordinary requests',
  ROUTING_PRIORITY_LARGE: 'Priority of large prompts',
  ROUTING_PRIORITY_ELEPHANT: 'Priority of very large prompts',
  UPSTREAM_COMPLETION_TIMEOUT_S: 'Non-streaming completion timeout (seconds)',
  STREAM_IDLE_TIMEOUT_SECONDS: 'Stream idle timeout (seconds)',
  STREAM_FIRST_BYTE_TIMEOUT_SECONDS: 'Stream first-frame timeout (seconds)',
  STREAM_MAX_IDLE_S: 'Messages stream idle timeout (seconds)',
  STREAM_MAX_FIRST_FRAME_IDLE_S: 'Messages stream first-frame timeout (seconds)',
  SMALL_MAXTOK_REASONING_TARGET: 'Model for tiny reasoning calls',
  SMALL_MAXTOK_REASONING_THRESHOLD: 'Largest tiny call (max_tokens)',
  SMALL_MAXTOK_REASONING_FLOOR: 'Output budget of a rerouted tiny call',
  HTTP_IDLE_TIMEOUT_SECONDS: 'Idle connection timeout (seconds)',
  REQUEST_TIMEOUT_SECONDS: 'Request timeout (seconds)',
  STREAM_REQUEST_TIMEOUT_SECONDS: 'Longest streaming response (seconds)',
  REQUEST_MAX_BODY_MB: 'Largest request body (MiB)',

  // --- batch inference ---
  BATCH_ENABLED: 'Batch inference',
  BATCH_MIN_ROLE: 'Lowest role that may submit batches',
  BATCH_MODEL_CONCURRENCY: 'Default batch cap per model',
  BATCH_YIELD_TO_INTERACTIVE: 'Yield to interactive requests',
  BATCH_MAX_REQUESTS: 'Requests per batch',
  BATCH_MAX_BODY_MB: 'Largest batch submission (MiB)',
  BATCH_MAX_ACTIVE_PER_USER: 'Active batches per user',
  BATCH_COMPLETION_WINDOW_HOURS: 'Completion window (hours)',
  BATCH_RETENTION_DAYS: 'Keep finished batches (days)',
  BATCH_SHARED_INTERACTIVE_LOAD: 'Count interactive load on every process',
  BATCH_MAX_QUEUED_REQUESTS_PER_USER: 'Unfinished batch requests per user',
  BATCH_ROLE_CONCURRENCY_FREE: 'Running batch requests per free user',
  BATCH_ROLE_CONCURRENCY_PRO: 'Running batch requests per pro user',
  BATCH_ROLE_CONCURRENCY_INTERNAL: 'Running batch requests per internal user',
  BATCH_ROLE_CONCURRENCY_ADMIN: 'Running batch requests per admin user',
  BATCH_INTERACTIVE_RESERVE: 'Slots kept for interactive requests',
  BATCH_QUEUED_ALERT_HOURS: 'Alert after waiting for capacity (hours)',
  BATCH_MAX_LINE_KB: 'Largest input line (KiB)',
  BATCH_MAX_RESULT_KB: 'Largest stored response (KiB)',
  BATCH_MAX_RESULTS_MB_PER_BATCH: 'Stored responses per batch (MiB)',
  BATCH_FILES_MAX_TOTAL_MB_PER_USER: 'Input files per user (MiB)',
  BATCH_FILE_UPLOADS_PER_HOUR: 'File uploads per user (hour)',
  BATCH_FILE_RETENTION_DAYS: 'Keep input files (days)',
  BATCH_ANTHROPIC_VERSIONS: 'Accepted anthropic-version values',
  BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS: 'Allow webhooks to private addresses',

  // --- network ---
  CORS_ALLOWED_ORIGINS: 'Allowed browser origins',
  TRUSTED_PROXIES: 'Trusted proxies',
  TRUSTED_DIRECT_CLIENT_NETWORKS: 'Networks of direct clients',
  TRUST_PROXY_HEADERS: 'Trust forwarding headers',
  TRUSTED_CLOUDFLARE_NETWORKS: 'Trusted Cloudflare networks',
  TRUST_CLOUDFLARE_HEADERS: 'Trust CF-Connecting-IP',
  TRUST_X_REAL_IP: 'Trust X-Real-IP',
  ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS: 'Allow private provider addresses',

  // --- alerts ---
  ALERTS_ENABLED: 'Alert engine',
  SLACK_ALERTS_WEBHOOK_URL: 'Slack alerts webhook',
  WECOM_ALERTS_WEBHOOK_URL: 'WeCom alerts webhook',
  SLACK_WEBHOOK_URL: 'Slack failed-request webhook',
  FAILED_REQUEST_ALERT_THRESHOLD: 'Failed requests that page',
  FAILED_REQUEST_ALERT_WINDOW_MINUTES: 'Failed-request window (minutes)',
  FAILED_REQUEST_ALERT_COOLDOWN_MINUTES: 'Minutes between pages',
  FAILED_REQUEST_ALERT_RATE: 'Failing share that pages',
  FAILED_REQUEST_ALERT_RATE_MIN_COUNT: 'Requests before the share counts',
  DEPLOYMENT_ENV: 'Environment name',
  ENVIRONMENT: 'Environment name (older setting)',
  ALERT_HISTORY_ENABLED: 'Alert history',
  ALERT_HISTORY_DEPLOYMENT_ID: 'Alert history deployment id',
  ALERT_HISTORY_READ_CREDENTIALS: 'Alert history read credentials',
  ALERT_HISTORY_WRITER_CREDENTIALS: 'Alert history writer credentials',

  // --- integrations ---
  IDENTITY_JWT_PRIVATE_KEY: 'Identity token signing key',
  IDENTITY_JWT_RETIRING_PUBLIC_KEYS: 'Retiring identity public keys',
  IDENTITY_ISSUER: 'Identity token issuer',
  IDENTITY_ALLOWED_REDIRECTS: 'Allowed identity callbacks',
  GATEWAY_GRANT_DISPATCH_TOKEN: 'Cloud agent control-plane token',
  OPENINFRA_INGEST_TOKEN: 'OpenInfra ingest token',
  OPENINFRA_ENGINE_ORDER: 'OpenInfra engine order',
  OPENINFRA_RETIRED_ENGINES: 'OpenInfra retired engines',
  OPENINFRA_RETENTION_DAYS: 'Keep OpenInfra samples (days)',
  OPENINFRA_DELETE_UNARCHIVED: 'Delete unarchived OpenInfra samples',
  API_KEY_AUTHORIZATION_CALLBACKS: 'Applications that may request API keys',
  RAG_API_KEY: 'Docs assistant API key',
  RAG_API_BASE_URL: 'Docs assistant gateway address',
  RAG_EMBEDDER: 'Docs assistant embedder',
  RAG_EMBED_MODEL: 'Docs assistant embedding model',
  RAG_CHAT_MODEL: 'Docs assistant answer model',
  RAG_TOP_K: 'Docs assistant passages per question',
  RAG_MAX_TOKENS: 'Docs assistant answer budget (tokens)',
  RAG_TEMPERATURE: 'Docs assistant temperature',
  RAG_INDEX_PATH: 'Docs assistant index',
  RAG_CORPUS_DIR: 'Docs assistant source',
  RAG_CHUNK_MAX_CHARS: 'Docs ingest chunk size (characters)',
  RAG_CHUNK_OVERLAP_CHARS: 'Docs ingest chunk overlap (characters)',
  RAG_GATEWAY_BASE_URL: 'Docs ingest gateway address',
  RAG_GATEWAY_API_KEY: 'Docs ingest API key',
  QDRANT_BASE_URL: 'Qdrant address',
  QDRANT_API_KEY: 'Qdrant API key',

  // --- privacy ---
  DB_STORE_FULL_CONTENT: 'Store request content',
  ERASURE_FENCE_SECRET: 'Erasure fence key',
  ERASURE_FENCE_PROTOCOL_READY: 'Allow permanent account deletion',
};

/** How the built-in providers' names are written, where capitals alone would not say. */
const PROVIDER_NAMES: Readonly<Record<string, string>> = {
  CHUTES: 'Chutes',
  DEEPSEEK: 'DeepSeek',
  FEATHERLESS: 'Featherless',
  MINIMAX: 'MiniMax',
  MOONSHOT: 'Moonshot (Kimi)',
  OLLAMA: 'Ollama',
  OPENROUTER: 'OpenRouter',
  STAGING: 'Staging',
  ZAI: 'Z.ai',
};

function providerName(prefix: string): string {
  return (
    PROVIDER_NAMES[prefix] ??
    prefix
      .toLowerCase()
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ')
  );
}

/** A provider's key or default base URL, by the name the backend gives it. */
const PROVIDER_SETTING = /^([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*?)_(API_KEY|BASE_URL)$/;

/**
 * The plain name Admin → Settings shows a setting under, or `undefined` for
 * one known only by its variable's name.
 */
export function configLabel(
  entry: Pick<ConfigEntry, 'key' | 'category'> & { origin?: ConfigEntry['origin'] },
): string | undefined {
  // An older backend says nothing of an entry's origin: its entries are built in.
  if (entry.origin !== undefined && entry.origin !== 'static') return undefined;
  // The email card's own names, for a backend whose email is listed as fields.
  if (entry.category === EMAIL_CATEGORY) {
    if (entry.key === EMAIL_PROVIDER_KEY) return 'Delivery service';
    const label = emailFieldLabel(entry.key);
    if (label !== entry.key) return label;
  }
  const known = LABELS[entry.key];
  if (known) return known;
  if (entry.category === 'providers') {
    const match = PROVIDER_SETTING.exec(entry.key);
    if (match) {
      return `${providerName(match[1])} ${match[2] === 'API_KEY' ? 'API key' : 'base URL'}`;
    }
  }
  return undefined;
}
