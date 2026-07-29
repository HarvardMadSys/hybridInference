'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { disconnectAgentIntegration, getAgentIntegrations } from '@/lib/api/agents';
import type { AgentIntegrationProvider, AgentIntegrationProviderApi } from '@/lib/api/agents';

const PROVIDERS: AgentIntegrationProvider[] = ['github', 'gitlab'];

const PROVIDER_COPY: Record<AgentIntegrationProvider, { name: string; description: string }> = {
  github: {
    name: 'GitHub',
    description: 'Connect GitHub for agent jobs and enhanced codebase context',
  },
  gitlab: {
    name: 'GitLab',
    description:
      'Connect GitLab to discover accessible projects. Agent jobs and merge requests are not supported yet.',
  },
};

function emptyProvider(provider: AgentIntegrationProvider): AgentIntegrationProviderApi {
  return {
    provider,
    configured: false,
    connected: false,
    connect_url: null,
    capabilities: [],
    accounts: [],
    repositories: [],
  };
}

function ProviderIcon({ provider }: { provider: AgentIntegrationProvider }) {
  return (
    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gray-100 text-gray-500">
      {provider === 'github' ? (
        <svg className="h-5 w-5" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
        </svg>
      ) : (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="m12 21.5-4.42-13.6h8.84L12 21.5Zm0 0L3.4 15.25l4.18-7.35L12 21.5Zm0 0 8.6-6.25-4.18-7.35L12 21.5ZM3.4 15.25l-1.1-3.38a.76.76 0 0 1 .03-.55L5.27 3.5a.38.38 0 0 1 .72.04L7.58 7.9 3.4 15.25Zm17.2 0 1.1-3.38a.76.76 0 0 0-.03-.55L18.73 3.5a.38.38 0 0 0-.72.04L16.42 7.9l4.18 7.35Z" />
        </svg>
      )}
    </span>
  );
}

function connectedDescription(provider: AgentIntegrationProviderApi): React.ReactNode {
  const accountLabels = provider.accounts.map((account) => account.label).filter(Boolean);
  const repositoryCount = provider.repositories.length;

  return (
    <>
      Connected
      {accountLabels.length > 0 ? ` as ${accountLabels.join(', ')}` : ''}
      {repositoryCount > 0
        ? ` · ${repositoryCount} ${repositoryCount === 1 ? 'repository' : 'repositories'}`
        : ''}
      {provider.provider === 'gitlab'
        ? ' · Project discovery only; Agent jobs and merge requests are not supported yet.'
        : ''}
    </>
  );
}

function ProviderActions({
  integration,
  disconnecting,
  onDisconnect,
}: {
  integration: AgentIntegrationProviderApi;
  disconnecting: string | null;
  onDisconnect: (connectionId: string, label: string) => void;
}) {
  const copy = PROVIDER_COPY[integration.provider];

  if (!integration.connected) {
    if (!integration.configured) {
      return <span className="text-sm text-gray-500">Admin setup required</span>;
    }
    return integration.connect_url ? (
      <a
        href={integration.connect_url}
        className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-medium text-gray-800 shadow-sm hover:bg-gray-50"
      >
        Connect
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path
            d="M7 13 13 7m0 0H8m5 0v5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </a>
    ) : (
      <span className="text-sm text-gray-400">Temporarily unavailable</span>
    );
  }

  return (
    <details className="group relative">
      <summary className="flex cursor-pointer list-none items-center gap-1 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-medium text-gray-800 shadow-sm hover:bg-gray-50 [&::-webkit-details-marker]:hidden">
        Manage
        <svg
          className="h-4 w-4 transition-transform group-open:rotate-180"
          viewBox="0 0 20 20"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="m7 8 3 3 3-3"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </summary>
      <div className="absolute right-0 z-10 mt-1 w-56 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg">
        {integration.connect_url ? (
          <a
            href={integration.connect_url}
            className="block rounded-lg px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            Reauthorize {copy.name}
          </a>
        ) : null}
        {integration.accounts.map((account) => (
          <button
            key={account.id}
            type="button"
            disabled={disconnecting === account.id}
            onClick={() => onDisconnect(account.id, account.label)}
            className="block w-full rounded-lg px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {disconnecting === account.id
              ? `Disconnecting ${account.label}…`
              : `Disconnect ${account.label}`}
          </button>
        ))}
      </div>
    </details>
  );
}

export function IntegrationsView({
  connectedProvider,
}: {
  connectedProvider?: AgentIntegrationProvider | null;
}) {
  const [providers, setProviders] = useState<AgentIntegrationProviderApi[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const connectedNoticeChecked = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAgentIntegrations();
      setProviders(response.providers);
      if (!connectedNoticeChecked.current && connectedProvider) {
        connectedNoticeChecked.current = true;
        const verified = response.providers.find(
          (provider) => provider.provider === connectedProvider && provider.connected,
        );
        if (verified) setNotice(`${PROVIDER_COPY[connectedProvider].name} connected.`);
      }
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not load integrations.');
    } finally {
      setLoading(false);
    }
  }, [connectedProvider]);

  useEffect(() => {
    void load();
  }, [load]);

  async function disconnect(
    provider: AgentIntegrationProvider,
    connectionId: string,
    label: string,
  ) {
    setDisconnecting(connectionId);
    setError(null);
    setNotice(null);
    try {
      await disconnectAgentIntegration(provider, connectionId);
      setNotice(`${label} disconnected.`);
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not disconnect the integration.');
    } finally {
      setDisconnecting(null);
    }
  }

  const providersByName = new Map(providers.map((provider) => [provider.provider, provider]));

  return (
    <section className="mx-auto w-full max-w-5xl px-6 py-12 lg:px-12 lg:py-16">
      <h1 className="text-3xl font-semibold tracking-tight text-gray-900">Integrations</h1>
      <p className="mt-2 text-base text-gray-500">
        Connect source control providers and extend your agents with MCP servers.
      </p>

      {notice ? (
        <div
          className="mt-6 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800"
          role="status"
        >
          {notice}
        </div>
      ) : null}
      {error ? (
        <div
          className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 font-medium hover:underline"
          >
            Try again
          </button>
        </div>
      ) : null}

      <div className="mt-10">
        <h2 className="text-sm font-medium text-gray-600">Source Control</h2>
        <div className="mt-4 rounded-2xl border border-gray-200 bg-white px-6 shadow-sm">
          {PROVIDERS.map((provider, index) => {
            const foundIntegration = providersByName.get(provider);
            const integration = foundIntegration ?? emptyProvider(provider);
            const statusUnavailable = Boolean(error && !foundIntegration);
            const copy = PROVIDER_COPY[provider];
            return (
              <div
                key={provider}
                className={`flex min-h-24 flex-col justify-center gap-4 py-5 sm:flex-row sm:items-center ${
                  index > 0 ? 'border-t border-gray-100' : ''
                }`}
              >
                <div className="flex min-w-0 flex-1 items-center gap-4">
                  <ProviderIcon provider={provider} />
                  <div className="min-w-0">
                    <h3 className="font-medium text-gray-900">{copy.name}</h3>
                    <p className="mt-0.5 text-sm leading-5 text-gray-500">
                      {loading
                        ? 'Loading connection status…'
                        : statusUnavailable
                          ? 'Connection status is unavailable'
                          : integration.error
                            ? integration.error
                            : integration.connected
                              ? connectedDescription(integration)
                              : copy.description}
                    </p>
                  </div>
                </div>
                <div className="flex shrink-0 justify-end pl-14 sm:pl-0">
                  {loading ? (
                    <span
                      className="h-8 w-24 animate-pulse rounded-lg bg-gray-100"
                      aria-label={`Loading ${copy.name}`}
                    />
                  ) : statusUnavailable ? (
                    <span className="text-sm text-gray-400">Unavailable</span>
                  ) : (
                    <ProviderActions
                      integration={integration}
                      disconnecting={disconnecting}
                      onDisconnect={(connectionId, label) =>
                        void disconnect(provider, connectionId, label)
                      }
                    />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-10">
        <h2 className="text-sm font-medium text-gray-600">MCP Servers</h2>
        <div className="mt-4 flex items-center gap-4 rounded-2xl border border-gray-200 bg-white px-6 py-5 shadow-sm">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gray-100 text-gray-500">
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M8 8.5 5.5 11a3.54 3.54 0 0 0 5 5l2.5-2.5m3-3L18.5 8a3.54 3.54 0 0 0-5-5L11 5.5m-3 6 8-8"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="font-medium text-gray-900">Custom MCP servers</h3>
            <p className="mt-0.5 text-sm text-gray-500">
              Team-managed MCP server connections are coming soon.
            </p>
          </div>
          <span className="rounded-full bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-500">
            Coming soon
          </span>
        </div>
      </div>
    </section>
  );
}
