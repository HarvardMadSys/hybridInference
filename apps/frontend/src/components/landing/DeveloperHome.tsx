'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { buildCurlExample, pickExampleModel } from '@/components/landing/curlExample';
import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

type GatewayStatus = 'checking' | 'healthy' | 'degraded' | 'unhealthy' | 'unreachable';

interface HealthResponse {
  status?: unknown;
}

interface ModelsResponse {
  data?: unknown;
}

interface HomeLink {
  href: string;
  label: string;
  primary?: boolean;
}

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

function readGatewayStatus(response: Response, payload: HealthResponse): GatewayStatus {
  if (payload.status === 'healthy') return 'healthy';
  if (payload.status === 'degraded') return 'degraded';
  if (payload.status === 'unhealthy') return 'unhealthy';
  return response.ok ? 'healthy' : 'unhealthy';
}

function readModelIds(payload: ModelsResponse): string[] {
  if (!Array.isArray(payload.data)) return [];
  return payload.data.flatMap((model) => {
    if (!model || typeof model !== 'object') return [];
    const id = (model as { id?: unknown }).id;
    return typeof id === 'string' && id.trim() ? [id] : [];
  });
}

// Anonymous visitors get the way in; signed-in users get the console. The
// header carries no sign-in link, so this is the page that has to offer one.
function homeLinks(auth: ReturnType<typeof useAuth>['state'], publicSignup: boolean): HomeLink[] {
  if (!auth.isAuthenticated) {
    const links: HomeLink[] = [{ href: '/login', label: 'Sign in', primary: true }];
    if (publicSignup) links.push({ href: '/signup', label: 'Sign up' });
    return links;
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

export function DeveloperHome(): JSX.Element {
  const { branding, distribution, features } = useSiteConfig();
  const { state: auth } = useAuth();
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus>('checking');
  const [models, setModels] = useState<string[] | null>(null);
  const [modelsUnavailable, setModelsUnavailable] = useState(false);
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();

    async function loadHealth(): Promise<void> {
      try {
        const response = await fetch('/health', {
          cache: 'no-store',
          signal: controller.signal,
        });
        const payload = (await response.json()) as HealthResponse;
        setGatewayStatus(readGatewayStatus(response, payload));
      } catch (error) {
        if (!(error instanceof Error && error.name === 'AbortError')) {
          setGatewayStatus('unreachable');
        }
      }
    }

    async function loadModels(): Promise<void> {
      try {
        const response = await fetch('/v1/models', {
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Models request failed with ${response.status}`);
        const payload = (await response.json()) as ModelsResponse;
        setModels(readModelIds(payload));
      } catch (error) {
        if (!(error instanceof Error && error.name === 'AbortError')) {
          setModelsUnavailable(true);
        }
      }
    }

    void loadHealth();
    void loadModels();

    return () => {
      controller.abort();
      window.clearTimeout(copiedTimer.current);
    };
  }, []);

  const isExample = distribution.id === 'example';
  const scope = isExample ? 'local gateway' : 'gateway';
  const needsAttention = gatewayStatus === 'unhealthy' || gatewayStatus === 'unreachable';
  const status = statusDetails[gatewayStatus];
  const exampleModel = pickExampleModel(models, branding.exampleModel);
  const curlExample = buildCurlExample({
    exampleApiBase: branding.exampleApiBase,
    exampleApiKeyEnvVar: branding.exampleApiKeyEnvVar,
    exampleModel,
  });
  const links = homeLinks(auth, features.publicSignup);

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
            {needsAttention ? `Your ${scope} needs attention.` : `Your ${scope} is ready.`}
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

        <dl className="grid border-t border-gray-200 bg-gray-50 sm:grid-cols-3">
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
          <div className="border-b border-gray-200 px-6 py-5 sm:border-b-0 sm:border-r sm:px-8">
            <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">API base</dt>
            <dd className="mt-2 break-all font-mono text-sm font-medium text-gray-900">
              {branding.exampleApiBase || 'Same origin'}
            </dd>
          </div>
          <div className="px-6 py-5 sm:px-8">
            <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Loaded models
            </dt>
            <dd className="mt-2 text-sm font-medium text-gray-900" aria-live="polite">
              {modelsUnavailable
                ? 'Unavailable'
                : models === null
                  ? 'Loading…'
                  : models.length > 0
                    ? models.join(', ')
                    : 'None configured'}
            </dd>
          </div>
        </dl>
      </section>

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
    </div>
  );
}
