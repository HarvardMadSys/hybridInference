'use client';

import Link from 'next/link';
import { useEffect, useId, useRef, useState } from 'react';

import { buildCurlExample, pickExampleModel } from '@/components/landing/curlExample';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { config } from '@/config/env';
import { fetchWithAuth } from '@/lib/api/client';

type GatewayStatus = 'checking' | 'healthy' | 'degraded' | 'unhealthy' | 'unreachable';

// Anonymous visitors see the public catalog; a signed-in user sees the models
// their account can reach, which /v1/models without credentials cannot tell.
type CatalogScope = 'public' | 'account';

interface JsonResult {
  ok: boolean;
  payload: unknown;
}

interface CatalogModel {
  id: string;
  chat: boolean;
}

interface HomeLink {
  href: string;
  label: string;
  primary?: boolean;
}

export const GATEWAY_REQUEST_TIMEOUT_MS = 8_000;
const MODEL_PREVIEW_LIMIT = 6;

const statusDetails: Record<GatewayStatus, { label: string; dotClassName: string }> = {
  checking: { label: 'Checking…', dotClassName: 'bg-gray-400' },
  healthy: { label: 'Healthy', dotClassName: 'bg-emerald-500' },
  degraded: { label: 'Degraded', dotClassName: 'bg-amber-500' },
  unhealthy: { label: 'Unhealthy', dotClassName: 'bg-red-500' },
  unreachable: { label: 'Unreachable', dotClassName: 'bg-red-500' },
};

const PRIMARY_LINK_CLASS =
  'inline-flex h-10 items-center justify-center rounded-lg bg-gray-950 px-4 text-sm font-medium text-white transition-colors hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-gray-900 focus:ring-offset-2';
const SECONDARY_LINK_CLASS =
  'inline-flex h-10 items-center justify-center rounded-lg border border-gray-300 bg-white px-4 text-sm font-medium text-gray-800 transition-colors hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2';

function readGatewayStatus(result: JsonResult): GatewayStatus {
  const status = (result.payload as { status?: unknown } | null)?.status;
  if (status === 'healthy') return 'healthy';
  if (status === 'degraded') return 'degraded';
  if (status === 'unhealthy') return 'unhealthy';
  return result.ok ? 'healthy' : 'unhealthy';
}

// The catalog lists embedding models beside chat models; the backend tags
// them with the "embeddings" feature so the quickstart never posts one to
// /v1/chat/completions.
function readCatalog(payload: unknown): CatalogModel[] {
  const data = (payload as { data?: unknown } | null)?.data;
  if (!Array.isArray(data)) return [];
  return data.flatMap((entry) => {
    if (!entry || typeof entry !== 'object') return [];
    const { id, supported_features: features } = entry as {
      id?: unknown;
      supported_features?: unknown;
    };
    if (typeof id !== 'string' || !id.trim()) return [];
    const chat = !(Array.isArray(features) && features.includes('embeddings'));
    return [{ id, chat }];
  });
}

// Runs `load` under a deadline that covers the whole exchange, body included:
// a gateway that answers the headers and then stalls the body must read as
// unreachable, not sit on "Checking…" for as long as the tab is open. The
// outer signal is the unmount abort.
async function withDeadline<T>(
  signal: AbortSignal,
  timeoutMs: number,
  load: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) abort();
  signal.addEventListener('abort', abort);
  const timer = window.setTimeout(abort, timeoutMs);
  try {
    return await load(controller.signal);
  } finally {
    window.clearTimeout(timer);
    signal.removeEventListener('abort', abort);
  }
}

async function readJson(response: Response): Promise<JsonResult> {
  return { ok: response.ok, payload: await response.json() };
}

function loadPublicJson(path: string, signal: AbortSignal): Promise<JsonResult> {
  return fetch(`${config.apiBase}${path}`, { cache: 'no-store', signal }).then(readJson);
}

function loadAccountJson(path: string, signal: AbortSignal): Promise<JsonResult> {
  return fetchWithAuth(config.apiBase, path, { cache: 'no-store', signal }).then(readJson);
}

function isAbsoluteUrl(value: string): boolean {
  return /^https?:\/\//i.test(value);
}

// Anonymous visitors get the way in; signed-in users get the console. The
// header carries no sign-in link, so this is the page that has to offer one.
function homeLinks(auth: ReturnType<typeof useAuth>['state'], publicSignup: boolean): HomeLink[] {
  if (!auth.isAuthenticated) {
    if (publicSignup) {
      return [
        { href: '/signup', label: 'Sign up', primary: true },
        { href: '/login', label: 'Sign in' },
      ];
    }
    return [{ href: '/login', label: 'Sign in', primary: true }];
  }
  const links: HomeLink[] = [{ href: '/dashboard', label: 'Dashboard', primary: true }];
  if (hasRole(auth.user?.role, 'internal')) {
    links.push({ href: '/dashboard/playground', label: 'Playground' });
  }
  if (auth.user?.is_admin) {
    links.push({ href: '/dashboard/admin', label: 'Admin Console' });
  }
  return links;
}

export function DeveloperHome({
  requestTimeoutMs = GATEWAY_REQUEST_TIMEOUT_MS,
}: {
  requestTimeoutMs?: number;
} = {}): JSX.Element {
  const { branding, distribution, features } = useSiteConfig();
  const { state: auth } = useAuth();
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus>('checking');
  const [catalog, setCatalog] = useState<CatalogModel[] | null>(null);
  const [catalogUnavailable, setCatalogUnavailable] = useState(false);
  const [showAllModels, setShowAllModels] = useState(false);
  const catalogId = useId();
  const [pageOrigin, setPageOrigin] = useState('');
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<number | undefined>(undefined);

  const catalogScope: CatalogScope = auth.isAuthenticated ? 'account' : 'public';

  useEffect(() => {
    setPageOrigin(window.location.origin);
    return () => window.clearTimeout(copiedTimer.current);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    withDeadline(signal, requestTimeoutMs, (inner) => loadPublicJson('/health', inner))
      .then((result) => setGatewayStatus(readGatewayStatus(result)))
      .catch(() => {
        if (!signal.aborted) setGatewayStatus('unreachable');
      });

    return () => controller.abort();
  }, [requestTimeoutMs]);

  useEffect(() => {
    // Fetch once the session has resolved, so the list is loaded for the
    // scope the visitor actually has rather than twice.
    if (auth.loading) return undefined;
    const controller = new AbortController();
    const { signal } = controller;
    setCatalog(null);
    setCatalogUnavailable(false);
    setShowAllModels(false);

    withDeadline(signal, requestTimeoutMs, (inner) =>
      catalogScope === 'account'
        ? loadAccountJson('/user/models', inner)
        : loadPublicJson('/v1/models', inner),
    )
      .then((result) => {
        if (!result.ok) throw new Error('Models request failed');
        setCatalog(readCatalog(result.payload));
      })
      .catch(() => {
        if (!signal.aborted) setCatalogUnavailable(true);
      });

    return () => controller.abort();
  }, [auth.loading, catalogScope, requestTimeoutMs]);

  const isExample = distribution.id === 'example';
  const scope = isExample ? 'local gateway' : 'gateway';
  const needsAttention = gatewayStatus === 'unhealthy' || gatewayStatus === 'unreachable';
  const status = statusDetails[gatewayStatus];
  const links = homeLinks(auth, features.publicSignup);
  const catalogLabel = catalogScope === 'account' ? 'Your models' : 'Public models';
  const visibleModels = showAllModels ? catalog : catalog?.slice(0, MODEL_PREVIEW_LIMIT);

  // The command needs an absolute base. A distribution that publishes one
  // wins; one that hides its example on purpose gets no command; otherwise
  // the base is the API origin this console is built against, else the page's
  // own origin, which proxies /v1. curl cannot resolve a relative URL.
  const quickstartHidden = branding.exampleHidden;
  const curlBase = quickstartHidden
    ? ''
    : branding.exampleApiBase || (isAbsoluteUrl(config.apiBase) ? config.apiBase : pageOrigin);
  const chatModels = catalog?.filter((model) => model.chat).map((model) => model.id) ?? null;
  const noChatModels = chatModels !== null && chatModels.length === 0;
  const exampleModel = pickExampleModel(chatModels, branding.exampleModel);
  const curlExample = buildCurlExample({
    exampleApiBase: curlBase,
    exampleApiKeyEnvVar: branding.exampleApiKeyEnvVar,
    exampleModel,
  });

  async function handleCopy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(curlExample);
      setCopied(true);
      window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // The command remains selectable when the Clipboard API is unavailable.
    }
  }

  return (
    <div className="flex w-full flex-col gap-6">
      <section className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
        <div className="px-6 py-10 sm:px-10 sm:py-12">
          {isExample ? (
            <div className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs font-medium text-gray-600">
              <span className="h-1.5 w-1.5 rounded-full bg-gray-500" aria-hidden="true" />
              Local example
            </div>
          ) : null}
          <h1
            className={`max-w-2xl text-3xl font-semibold tracking-tight text-gray-950 sm:text-5xl ${
              isExample ? 'mt-5' : ''
            }`}
          >
            {/* /health is a liveness check (process and stores), so this says
                "running", not "ready": provider circuits are not consulted. */}
            {needsAttention ? `Your ${scope} needs attention.` : `Your ${scope} is running.`}
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-7 text-gray-600">
            {isExample
              ? 'Inspect the example deployment, try a request, or open the console to configure your own routes.'
              : 'Check the gateway, try a request, or open the console to configure routes and keys.'}
          </p>

          <nav aria-label="Gateway tools" className="mt-7 flex flex-wrap gap-3">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={link.primary ? PRIMARY_LINK_CLASS : SECONDARY_LINK_CLASS}
              >
                {link.label}
              </Link>
            ))}
          </nav>
        </div>

        <dl className="grid border-t border-gray-200 bg-gray-50 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <div className="border-b border-gray-200 px-6 py-5 sm:border-b-0 sm:border-r sm:px-8">
            <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Gateway health
            </dt>
            <dd
              className="mt-2 flex items-center gap-2 text-sm font-semibold text-gray-900"
              aria-live="polite"
            >
              <span className={`h-2 w-2 rounded-full ${status.dotClassName}`} aria-hidden="true" />
              {status.label}
            </dd>
          </div>
          <div className="min-w-0 px-6 py-5 sm:px-8">
            <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">API base</dt>
            <dd className="mt-2 break-all font-mono text-sm font-medium text-gray-900">
              {quickstartHidden ? 'Not published' : curlBase || 'Same origin'}
            </dd>
          </div>
        </dl>
        <section
          aria-labelledby={`${catalogId}-heading`}
          className="border-t border-gray-200 px-6 py-5 sm:px-8"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <h2
                id={`${catalogId}-heading`}
                className="text-xs font-medium uppercase tracking-wide text-gray-500"
              >
                {catalogLabel}
              </h2>
              {catalog !== null && !catalogUnavailable && (
                <span className="rounded-md bg-gray-100 px-1.5 py-0.5 text-xs font-medium tabular-nums text-gray-600">
                  {catalog.length}
                </span>
              )}
            </div>
            {catalog !== null && catalog.length > MODEL_PREVIEW_LIMIT && (
              <button
                type="button"
                aria-expanded={showAllModels}
                aria-controls={catalogId}
                onClick={() => setShowAllModels((expanded) => !expanded)}
                className="rounded-md px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-950 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
              >
                {showAllModels ? 'Show fewer' : `Show all ${catalog.length}`}
              </button>
            )}
          </div>
          <div className="mt-3 text-sm text-gray-600" aria-live="polite">
            {catalogUnavailable ? (
              'Unavailable'
            ) : catalog === null ? (
              'Loading…'
            ) : catalog.length > 0 ? (
              <ul
                id={catalogId}
                aria-label={catalogLabel}
                tabIndex={showAllModels ? 0 : undefined}
                className={`flex flex-wrap gap-2 rounded-md focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-inset ${showAllModels ? 'max-h-60 overflow-y-auto' : ''}`}
              >
                {visibleModels?.map((model) => (
                  <li
                    key={model.id}
                    className="min-w-0 max-w-full rounded-md border border-gray-200 bg-gray-50 px-2.5 py-1.5"
                  >
                    <code className="break-all text-xs leading-5 text-gray-700">{model.id}</code>
                  </li>
                ))}
              </ul>
            ) : (
              'None configured'
            )}
          </div>
        </section>
      </section>

      {noChatModels ? (
        <section className="rounded-2xl border border-dashed border-gray-300 bg-white px-6 py-6 text-sm text-gray-600">
          {catalogScope === 'public' ? (
            <>
              <p className="font-medium text-gray-900">No public chat models.</p>
              <p className="mt-1">Sign in to see the models available to your account.</p>
            </>
          ) : auth.user?.is_admin ? (
            <>
              <p className="font-medium text-gray-900">No chat models yet.</p>
              <p className="mt-1">
                Add a route in the{' '}
                <Link href="/dashboard/admin" className="underline hover:text-gray-900">
                  Admin Console
                </Link>{' '}
                to serve one.
              </p>
            </>
          ) : (
            <>
              <p className="font-medium text-gray-900">
                No chat models are available to your account.
              </p>
              <p className="mt-1">
                Ask the operator of this deployment to add a route or grant access.
              </p>
            </>
          )}
        </section>
      ) : curlBase ? (
        <section className="overflow-hidden rounded-2xl border border-gray-200 bg-gray-950 shadow-sm">
          <div className="flex items-center justify-between border-b border-gray-800 px-5 py-3">
            <div>
              <p className="text-sm font-medium text-white">Try the API</p>
              <p className="mt-0.5 text-xs text-gray-400">
                Uses <code>{exampleModel}</code>
              </p>
            </div>
            <button
              type="button"
              onClick={handleCopy}
              className="rounded-md border border-gray-700 px-3 py-1.5 text-xs font-medium text-gray-300 transition-colors hover:border-gray-600 hover:bg-gray-900 hover:text-white focus:outline-none focus:ring-2 focus:ring-gray-500"
              aria-live="polite"
            >
              {copied ? 'Copied' : 'Copy curl'}
            </button>
          </div>
          <pre className="overflow-x-auto px-5 py-5 text-sm leading-6 text-gray-200">
            <code>{curlExample}</code>
          </pre>
        </section>
      ) : null}
    </div>
  );
}
