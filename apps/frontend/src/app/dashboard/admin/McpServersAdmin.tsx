'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import { McpServerIcon } from '@/components/features/mcp/McpServerIcon';
import {
  deleteAdminMcpServer,
  listAdminMcpServers,
  type AdminMcpServer,
} from '@/lib/api/mcpServers';
import { McpServerForm } from './McpServerForm';
import { MCP_FIELD } from './mcpServerDraft';

function headerNote(server: AdminMcpServer): string {
  const count = Object.keys(server.headers).length;
  const headers =
    count === 0 ? 'No headers' : `${count} header${count === 1 ? '' : 's'} from secret settings`;
  return server.docs_url ? `${headers} · Has docs link` : headers;
}

export function McpServersAdmin() {
  const [servers, setServers] = useState<AdminMcpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<AdminMcpServer | 'new' | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listAdminMcpServers()
      .then(setServers)
      .catch((err: unknown) =>
        setError(err instanceof Error && err.message ? err.message : 'Request failed'),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return servers;
    return servers.filter((server) =>
      [server.name, server.id, server.url, server.description].some((value) =>
        value.toLowerCase().includes(needle),
      ),
    );
  }, [servers, query]);

  const remove = async (server: AdminMcpServer) => {
    setRemoving(server.id);
    setError(null);
    try {
      setServers(await deleteAdminMcpServer(server.id));
      setConfirming(null);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : 'Could not remove the server');
    } finally {
      setRemoving(null);
    }
  };

  return (
    <>
      <AdminSection
        title="MCP servers"
        badge={
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium tabular-nums text-gray-600">
            {servers.length}
          </span>
        }
        description="The Playground's store. Picking one fills in its connection details; headers stay as secret-setting references."
        actions={
          <button
            type="button"
            onClick={() => setEditing('new')}
            className="rounded-lg bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-800"
          >
            + Add server
          </button>
        }
      >
        {error && (
          <div className="mb-3">
            <AdminNotice
              role="alert"
              action={
                <button
                  type="button"
                  onClick={load}
                  className="font-medium underline underline-offset-2"
                >
                  Retry
                </button>
              }
            >
              {error}
            </AdminNotice>
          </div>
        )}

        <div className="mb-3 max-w-sm">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search servers"
            aria-label="Search MCP servers"
            className={MCP_FIELD}
          />
        </div>

        {loading ? (
          <AdminLoading label="Loading MCP servers..." />
        ) : servers.length === 0 ? (
          <AdminEmptyState title="No MCP servers in the store.">
            Add one to offer it in the Playground.
          </AdminEmptyState>
        ) : visible.length === 0 ? (
          <AdminEmptyState title="No servers match that search." />
        ) : (
          <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200 bg-white">
            {visible.map((server) => (
              <li
                key={server.id}
                className="flex flex-wrap items-start gap-3 px-4 py-3 sm:flex-nowrap"
              >
                <McpServerIcon
                  name={server.name}
                  url={server.url}
                  iconUrl={server.icon_url}
                  size="lg"
                />
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold text-gray-900">
                    {server.name}{' '}
                    <span className="ml-1 font-mono text-[11px] font-normal text-gray-400">
                      {server.id}
                    </span>
                  </p>
                  <p className="mt-0.5 break-all text-[12px] text-gray-500">{server.url}</p>
                  {server.description && (
                    <p className="mt-0.5 text-[12px] text-gray-500">{server.description}</p>
                  )}
                  <p className="mt-1 text-[11px] text-gray-400">{headerNote(server)}</p>
                </div>
                <div className="ml-auto flex shrink-0 items-center gap-2">
                  {confirming === server.id ? (
                    <>
                      <span className="text-[11px] text-gray-500">Remove?</span>
                      <button
                        type="button"
                        onClick={() => void remove(server)}
                        disabled={removing === server.id}
                        className="rounded-md bg-red-600 px-2.5 py-1 text-[12px] font-medium text-white transition hover:bg-red-700 disabled:opacity-50"
                      >
                        {removing === server.id ? 'Removing...' : 'Remove'}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirming(null)}
                        disabled={removing === server.id}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-600 transition hover:bg-gray-100"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => setEditing(server)}
                        className="rounded-md border border-gray-200 px-2.5 py-1 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirming(server.id)}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-500 transition hover:bg-gray-100 hover:text-red-600"
                      >
                        Remove
                      </button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </AdminSection>

      {editing && (
        <McpServerForm
          server={editing === 'new' ? undefined : editing}
          onCancel={() => setEditing(null)}
          onSaved={(next) => {
            setServers(next);
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
