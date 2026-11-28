'use client';

import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Select } from '@/components/ui/Select';
import { McpStorePicker, type McpCatalogEntry } from './McpStorePicker';

export type McpAuthMode = 'none' | 'token' | 'headers';

export interface McpHeaderRow {
  name: string;
  value: string;
}

export interface McpConnectRequest {
  url: string;
  description: string;
  headers: Record<string, string>;
  /** Set when the request came from the store; the gateway resolves it. */
  catalogId?: string;
  name?: string;
}

const AUTH_OPTIONS: { value: McpAuthMode; label: string }[] = [
  { value: 'none', label: 'None' },
  { value: 'token', label: 'Access token / API key' },
  { value: 'headers', label: 'Custom headers' },
];

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The request headers an auth choice sends. A bare token becomes a Bearer
 * Authorization header; one that already names its scheme is sent as typed.
 */
export function mcpAuthHeaders(
  mode: McpAuthMode,
  token: string,
  rows: McpHeaderRow[],
): Record<string, string> {
  if (mode === 'token') {
    const value = token.trim();
    if (!value) return {};
    return { Authorization: /^bearer\s/i.test(value) ? value : `Bearer ${value}` };
  }
  if (mode === 'headers') {
    const headers: Record<string, string> = {};
    for (const row of rows) {
      const name = row.name.trim();
      const value = row.value.trim();
      if (name && value) headers[name] = value;
    }
    return headers;
  }
  return {};
}

function EyeIcon({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
      {!open && <path d="M3 3l18 18" />}
    </svg>
  );
}

function SecretInput({
  value,
  onChange,
  placeholder,
  label,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
  disabled: boolean;
}) {
  const [shown, setShown] = useState(false);
  return (
    <div className="relative">
      <input
        type={shown ? 'text' : 'password'}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={label}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        className="w-full rounded-lg border border-gray-200 bg-white py-2 pl-3 pr-9 text-sm text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50"
      />
      <button
        type="button"
        onClick={() => setShown((current) => !current)}
        aria-label={shown ? `Hide ${label}` : `Show ${label}`}
        aria-pressed={shown}
        disabled={disabled}
        className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-gray-400 transition hover:text-gray-700 disabled:opacity-40"
      >
        <EyeIcon open={shown} />
      </button>
    </div>
  );
}

/**
 * Connect to an existing MCP server: URL, optional description and how to
 * authenticate. Mounted only while open, so every opening starts empty.
 * `onConnect` rejects with the message to show; on success the caller closes.
 */
export function McpConnectDialog({
  onClose,
  onConnect,
}: {
  onClose: () => void;
  onConnect: (request: McpConnectRequest) => Promise<void>;
}) {
  const ids = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const urlRef = useRef<HTMLInputElement>(null);
  const [url, setUrl] = useState('');
  const [description, setDescription] = useState('');
  const [mode, setMode] = useState<McpAuthMode>('none');
  const [token, setToken] = useState('');
  const [rows, setRows] = useState<McpHeaderRow[]>([{ name: '', value: '' }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    urlRef.current?.focus();
  }, []);

  const close = () => {
    if (!busy) onClose();
  };

  const pickCatalogServer = async (entry: McpCatalogEntry) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConnect({
        url: entry.url,
        description: entry.description,
        headers: {},
        catalogId: entry.id,
        name: entry.name,
      });
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : 'Could not reach the server');
      setBusy(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const target = url.trim();
    if (!target || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConnect({
        url: target,
        description: description.trim(),
        headers: mcpAuthHeaders(mode, token, rows),
      });
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : 'Could not reach the server');
      setBusy(false);
    }
  };

  // The dialog owns Escape and Tab: stopping them here keeps a panel behind
  // it (the mobile configuration drawer) from closing or taking focus.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      if (event.defaultPrevented) return; // a picker inside closed itself
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab' || !panelRef.current) return;
    event.stopPropagation();
    const items = [...panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const fieldLabel = 'block text-[13px] font-medium text-gray-900';
  const hint = 'mt-0.5 text-xs text-gray-500';
  const control =
    'mt-1.5 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50';

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-[10vh]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${ids}-title`}
        onKeyDown={onKeyDown}
        className="w-full max-w-lg rounded-2xl border border-gray-200 bg-white shadow-2xl"
      >
        <form onSubmit={(event) => void submit(event)}>
          <div className="flex flex-col items-center px-6 pt-6">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-gray-200 text-gray-700">
              <svg
                aria-hidden="true"
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.8}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M9 7V3M15 7V3M6 7h12v4a6 6 0 0 1-12 0V7zM12 17v4" />
              </svg>
            </div>
            <h2 id={`${ids}-title`} className="mt-3 text-base font-semibold text-gray-900">
              Add MCP server
            </h2>
            <p className="mt-1 text-center text-xs text-gray-500">
              Pick a server from this deployment&apos;s store, or connect your own.
            </p>
          </div>

          <div className="space-y-4 px-6 py-5">
            <div>
              <span className={fieldLabel}>Store</span>
              <p className={hint}>
                Servers this deployment offers. Their credentials stay on the gateway.
              </p>
              <div className="mt-2">
                <McpStorePicker onPick={(entry) => void pickCatalogServer(entry)} busy={busy} />
              </div>
            </div>

            <div className="flex items-center gap-3">
              <span className="h-px flex-1 bg-gray-100" />
              <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400">
                Custom server
              </span>
              <span className="h-px flex-1 bg-gray-100" />
            </div>

            <div>
              <label htmlFor={`${ids}-url`} className={fieldLabel}>
                URL
              </label>
              <p id={`${ids}-url-hint`} className={hint}>
                Only use MCP servers you trust and verify.
              </p>
              <input
                ref={urlRef}
                id={`${ids}-url`}
                type="text"
                inputMode="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://mcp.example.com/mcp"
                aria-label="MCP server URL"
                aria-describedby={`${ids}-url-hint`}
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
                className={control}
              />
            </div>

            <div>
              <label htmlFor={`${ids}-description`} className={fieldLabel}>
                Description <span className="font-normal text-gray-400">(optional)</span>
              </label>
              <input
                id={`${ids}-description`}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="My MCP server"
                maxLength={200}
                disabled={busy}
                className={control}
              />
            </div>

            <div>
              <span id={`${ids}-auth`} className={fieldLabel}>
                Authentication
              </span>
              <Select
                id={`${ids}-auth-select`}
                size="lg"
                className="mt-1.5 w-full"
                aria-labelledby={`${ids}-auth`}
                value={mode}
                onValueChange={(value) => setMode(value as McpAuthMode)}
                disabled={busy}
                options={AUTH_OPTIONS}
              />
              {mode === 'token' && (
                <div className="mt-2">
                  <SecretInput
                    value={token}
                    onChange={setToken}
                    placeholder="Add your access token"
                    label="access token"
                    disabled={busy}
                  />
                  <p className={hint}>Sent as an Authorization: Bearer header.</p>
                </div>
              )}
              {mode === 'headers' && (
                <div className="mt-2 space-y-2">
                  {rows.map((row, index) => (
                    <div key={index} className="flex items-center gap-2">
                      <input
                        value={row.name}
                        onChange={(event) =>
                          setRows((current) =>
                            current.map((item, i) =>
                              i === index ? { ...item, name: event.target.value } : item,
                            ),
                          )
                        }
                        placeholder="Header name"
                        aria-label={`Header ${index + 1} name`}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={busy}
                        className="w-2/5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50"
                      />
                      <div className="min-w-0 flex-1">
                        <SecretInput
                          value={row.value}
                          onChange={(value) =>
                            setRows((current) =>
                              current.map((item, i) => (i === index ? { ...item, value } : item)),
                            )
                          }
                          placeholder="Value"
                          label={`header ${index + 1} value`}
                          disabled={busy}
                        />
                      </div>
                      <button
                        type="button"
                        onClick={() =>
                          setRows((current) =>
                            current.length === 1
                              ? [{ name: '', value: '' }]
                              : current.filter((_, i) => i !== index),
                          )
                        }
                        aria-label={`Remove header ${index + 1}`}
                        disabled={busy}
                        className="shrink-0 rounded p-1 text-gray-400 transition hover:text-gray-800 disabled:opacity-40"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={() => setRows((current) => [...current, { name: '', value: '' }])}
                    disabled={busy}
                    className="text-xs font-medium text-gray-600 transition hover:text-gray-900 disabled:opacity-40"
                  >
                    + Add header
                  </button>
                </div>
              )}
            </div>

            {error && (
              <p role="alert" className="break-words text-xs text-red-600">
                {error}
              </p>
            )}
          </div>

          <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-6 py-4">
            <button
              type="button"
              onClick={close}
              disabled={busy}
              className="rounded-lg bg-gray-100 px-4 py-2 text-sm font-medium text-gray-700 transition hover:bg-gray-200 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!url.trim() || busy}
              className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-gray-800 disabled:opacity-40"
            >
              {busy ? 'Connecting...' : 'Connect'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
