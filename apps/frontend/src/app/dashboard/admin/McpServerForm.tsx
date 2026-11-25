'use client';

import { useId, useState, type FormEvent, type KeyboardEvent } from 'react';
import { AdminSpinner } from '@/components/features/admin/AdminPrimitives';
import { McpServerIcon } from '@/components/features/mcp/McpServerIcon';
import {
  createAdminMcpServer,
  updateAdminMcpServer,
  type AdminMcpServer,
} from '@/lib/api/mcpServers';
import { MCP_FIELD, slug, toDraft, toInput, validate, type Draft } from './mcpServerDraft';

export function McpServerForm({
  server,
  onCancel,
  onSaved,
}: {
  server?: AdminMcpServer;
  onCancel: () => void;
  onSaved: (servers: AdminMcpServer[]) => void;
}) {
  const ids = useId();
  const [draft, setDraft] = useState<Draft>(() => toDraft(server));
  const [idTouched, setIdTouched] = useState(Boolean(server));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const patch = (next: Partial<Draft>) => setDraft((current) => ({ ...current, ...next }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const problem = validate(draft);
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const input = toInput(draft);
      const servers = server
        ? await updateAdminMcpServer(server.id, input)
        : await createAdminMcpServer(input);
      onSaved(servers);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : 'Could not save the server');
      setSaving(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !saving) {
      event.preventDefault();
      onCancel();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-[8vh]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${ids}-title`}
        onKeyDown={onKeyDown}
        className="w-full max-w-xl rounded-2xl border border-gray-200 bg-white shadow-2xl"
      >
        <form onSubmit={(event) => void submit(event)}>
          <div className="flex items-center gap-3 border-b border-gray-100 px-6 py-4">
            <McpServerIcon
              name={draft.name || 'Server'}
              url={draft.url}
              iconUrl={draft.iconUrl}
              size="lg"
            />
            <div className="min-w-0">
              <h2 id={`${ids}-title`} className="text-base font-semibold text-gray-900">
                {server ? 'Edit MCP server' : 'Add MCP server'}
              </h2>
              <p className="text-[12px] text-gray-500">
                Offered in the Playground store. Header values reference secret settings.
              </p>
            </div>
          </div>

          <div className="max-h-[65vh] space-y-4 overflow-y-auto px-6 py-5">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-[13px] font-medium text-gray-900">
                Name
                <input
                  value={draft.name}
                  onChange={(event) => {
                    const name = event.target.value;
                    patch({
                      name,
                      ...(idTouched ? {} : { id: slug(name) }),
                    });
                  }}
                  aria-label="Name"
                  placeholder="Context7"
                  maxLength={80}
                  disabled={saving}
                  className={MCP_FIELD}
                />
              </label>
              <label className="text-[13px] font-medium text-gray-900">
                Id
                <input
                  value={draft.id}
                  onChange={(event) => {
                    setIdTouched(true);
                    patch({ id: event.target.value });
                  }}
                  aria-label="Id"
                  placeholder="context7"
                  maxLength={64}
                  disabled={saving || Boolean(server)}
                  className={MCP_FIELD}
                />
              </label>
            </div>

            <label className="block text-[13px] font-medium text-gray-900">
              URL
              <input
                type="url"
                value={draft.url}
                onChange={(event) => patch({ url: event.target.value })}
                aria-label="URL"
                placeholder="https://mcp.example.com/mcp"
                disabled={saving}
                className={MCP_FIELD}
              />
            </label>

            <label className="block text-[13px] font-medium text-gray-900">
              Description
              <input
                value={draft.description}
                onChange={(event) => patch({ description: event.target.value })}
                aria-label="Description"
                placeholder="What this server provides."
                maxLength={500}
                disabled={saving}
                className={MCP_FIELD}
              />
            </label>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-[13px] font-medium text-gray-900">
                Docs URL
                <input
                  type="url"
                  value={draft.docsUrl}
                  onChange={(event) => patch({ docsUrl: event.target.value })}
                  aria-label="Docs URL"
                  placeholder="https://example.com/docs"
                  disabled={saving}
                  className={MCP_FIELD}
                />
              </label>
              <label className="text-[13px] font-medium text-gray-900">
                Icon URL
                <input
                  type="url"
                  value={draft.iconUrl}
                  onChange={(event) => patch({ iconUrl: event.target.value })}
                  aria-label="Icon URL"
                  placeholder="Defaults to the server's favicon"
                  disabled={saving}
                  className={MCP_FIELD}
                />
              </label>
            </div>

            <div>
              <span className="text-[13px] font-medium text-gray-900">Headers</span>
              <p className="mt-0.5 text-xs text-gray-500">
                A value references a separate secret setting as <code>{'${NAME}'}</code>.
              </p>
              <div className="mt-2 space-y-2">
                {draft.headers.map((row, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <input
                      value={row.name}
                      onChange={(event) =>
                        patch({
                          headers: draft.headers.map((item, i) =>
                            i === index ? { ...item, name: event.target.value } : item,
                          ),
                        })
                      }
                      placeholder="Authorization"
                      aria-label={`Header ${index + 1} name`}
                      disabled={saving}
                      className={MCP_FIELD}
                    />
                    <input
                      value={row.value}
                      onChange={(event) =>
                        patch({
                          headers: draft.headers.map((item, i) =>
                            i === index ? { ...item, value: event.target.value } : item,
                          ),
                        })
                      }
                      placeholder="${MCP_TOKEN}"
                      aria-label={`Header ${index + 1} value`}
                      disabled={saving}
                      className={MCP_FIELD}
                    />
                    <button
                      type="button"
                      onClick={() =>
                        patch({
                          headers:
                            draft.headers.length === 1
                              ? [{ name: '', value: '' }]
                              : draft.headers.filter((_, i) => i !== index),
                        })
                      }
                      aria-label={`Remove header ${index + 1}`}
                      disabled={saving}
                      className="shrink-0 rounded p-1 text-gray-400 transition hover:text-gray-800 disabled:opacity-40"
                    >
                      ×
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => patch({ headers: [...draft.headers, { name: '', value: '' }] })}
                  disabled={saving}
                  className="text-xs font-medium text-gray-600 transition hover:text-gray-900 disabled:opacity-40"
                >
                  + Add header
                </button>
              </div>
            </div>

            {error && (
              <p role="alert" className="break-words text-xs text-red-600">
                {error}
              </p>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-gray-100 px-6 py-4">
            <button
              type="button"
              onClick={onCancel}
              disabled={saving}
              className="rounded-lg bg-gray-100 px-4 py-2 text-sm font-medium text-gray-700 transition hover:bg-gray-200 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="inline-flex items-center gap-2 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-gray-800 disabled:opacity-40"
            >
              {saving && <AdminSpinner size="sm" />}
              {server ? 'Save changes' : 'Add server'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
