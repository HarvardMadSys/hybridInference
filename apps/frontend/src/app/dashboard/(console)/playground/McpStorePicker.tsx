'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { McpServerIcon } from '@/components/features/mcp/McpServerIcon';
import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';

export interface McpCatalogEntry {
  id: string;
  name: string;
  description: string;
  url: string;
  docsUrl?: string;
  iconUrl?: string;
}

const API_BASE = config.apiBase;

/**
 * The deployment's MCP server store, fetched when the add dialog opens. A
 * failed load leaves the manual form as the fallback, so it never blocks
 * connecting a server.
 */
export function McpStorePicker({
  onPick,
  busy,
}: {
  onPick: (entry: McpCatalogEntry) => void;
  busy: boolean;
}) {
  const [entries, setEntries] = useState<McpCatalogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  const load = useCallback(() => {
    let cancelled = false;
    setError(null);
    setEntries(null);
    fetchWithAuth(API_BASE, '/internal/playground/mcp/servers')
      .then((resp) => jsonOrThrow<{ servers: McpCatalogEntry[] }>(resp))
      .then((data) => {
        if (!cancelled) setEntries(Array.isArray(data.servers) ? data.servers : []);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error && err.message ? err.message : 'Request failed');
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(load, [load]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!entries || !needle) return entries ?? [];
    return entries.filter((entry) =>
      [entry.name, entry.description, entry.url].some((value) =>
        value.toLowerCase().includes(needle),
      ),
    );
  }, [entries, query]);

  if (error) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        <span>The server store is unavailable. You can still connect a custom server.</span>
        <button
          type="button"
          onClick={load}
          className="ml-2 font-medium underline underline-offset-2"
        >
          Retry
        </button>
      </div>
    );
  }

  if (entries === null) {
    return <p className="py-3 text-xs text-gray-500">Loading servers...</p>;
  }

  if (entries.length === 0) {
    return (
      <p className="text-xs text-gray-500">
        No servers are in this deployment&apos;s store. Connect a custom server below.
      </p>
    );
  }

  return (
    <div>
      <input
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search all servers"
        aria-label="Search MCP servers"
        disabled={busy}
        className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50"
      />
      <p className="mb-2 mt-4 text-[11px] font-medium uppercase tracking-wide text-gray-400">
        Available servers
      </p>
      <ul className="grid max-h-64 grid-cols-1 gap-2 overflow-y-auto sm:grid-cols-2">
        {visible.map((entry) => (
          <li key={entry.id}>
            <button
              type="button"
              onClick={() => onPick(entry)}
              disabled={busy}
              className="flex h-full w-full items-start gap-2.5 rounded-xl border border-gray-200 px-3 py-2.5 text-left transition hover:border-gray-300 hover:bg-gray-50 disabled:opacity-50"
            >
              <McpServerIcon name={entry.name} url={entry.url} iconUrl={entry.iconUrl} size="md" />
              <span className="min-w-0">
                <span className="block truncate text-[13px] font-medium text-gray-900">
                  {entry.name}
                </span>
                <span className="mt-0.5 block break-words text-[11px] leading-4 text-gray-500">
                  {entry.description || entry.url}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {visible.length === 0 && (
        <p className="mt-2 text-xs text-gray-500">No servers match that search.</p>
      )}
    </div>
  );
}
