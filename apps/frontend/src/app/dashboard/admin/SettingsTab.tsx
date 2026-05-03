'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  SignupAllowedDomain,
  addSignupAllowedDomain,
  listSignupAllowedDomains,
  listRuntimeSettings,
  removeSignupAllowedDomain,
  RuntimeSettingItem,
  updateRuntimeSetting,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { validateSignupDomainInput } from './signupDomainValidation';

function relTime(iso: string | null): string {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

export function SettingsTab() {
  const [domains, setDomains] = useState<SignupAllowedDomain[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<SignupAllowedDomain | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [featureFlags, setFeatureFlags] = useState<RuntimeSettingItem[]>([]);
  const [flagsLoading, setFlagsLoading] = useState(true);
  const [flagsError, setFlagsError] = useState<string | null>(null);
  const [togglingKey, setTogglingKey] = useState<string | null>(null);

  const loadDomains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listSignupAllowedDomains();
      setDomains(resp.domains);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadFlags = useCallback(async () => {
    setFlagsLoading(true);
    setFlagsError(null);
    try {
      const resp = await listRuntimeSettings();
      setFeatureFlags(resp.settings.filter((s) => s.value_type === 'bool'));
    } catch (e) {
      setFlagsError(getErrorMessage(e));
    } finally {
      setFlagsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDomains();
    loadFlags();
  }, [loadDomains, loadFlags]);

  useEffect(
    () => () => {
      if (toastTimeoutRef.current !== null) {
        clearTimeout(toastTimeoutRef.current);
        toastTimeoutRef.current = null;
      }
    },
    [],
  );

  const flashToast = (msg: string) => {
    if (toastTimeoutRef.current !== null) {
      clearTimeout(toastTimeoutRef.current);
    }
    setToast(msg);
    toastTimeoutRef.current = setTimeout(() => {
      setToast(null);
      toastTimeoutRef.current = null;
    }, 3000);
  };

  const onToggleFlag = async (flag: RuntimeSettingItem) => {
    setTogglingKey(flag.key);
    try {
      await updateRuntimeSetting(flag.key, !flag.value);
      flashToast(`${flag.key} ${!flag.value ? 'enabled' : 'disabled'}`);
      await loadFlags();
    } catch (e) {
      flashToast(`Failed to update ${flag.key}: ${getErrorMessage(e)}`);
    } finally {
      setTogglingKey(null);
    }
  };

  const onAdd = async () => {
    setInputError(null);
    const validated = validateSignupDomainInput(input);
    if (!validated.ok) {
      setInputError(validated.error);
      return;
    }
    setAdding(true);
    try {
      await addSignupAllowedDomain(input.trim());
      setInput('');
      flashToast(
        validated.isWildcard
          ? `Added wildcard *.${validated.domain}.`
          : `Added ${validated.domain}.`,
      );
      await loadDomains();
    } catch (e) {
      setInputError(getErrorMessage(e));
    } finally {
      setAdding(false);
    }
  };

  const onRemove = async (row: SignupAllowedDomain) => {
    const key = `${row.domain}|${row.is_wildcard}`;
    setRemoving(key);
    try {
      await removeSignupAllowedDomain(row.domain, row.is_wildcard);
      flashToast(row.is_wildcard ? `Removed wildcard *.${row.domain}.` : `Removed ${row.domain}.`);
      await loadDomains();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setRemoving(null);
      setConfirm(null);
    }
  };

  return (
    <div className="mt-5 space-y-6">
      {/* Feature Flags */}
      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <div className="mb-3">
          <h2 className="text-[14px] font-semibold text-gray-900">Feature Flags</h2>
          <p className="mt-1 text-[12px] text-gray-500">
            Toggle runtime features without restarting the server. Changes take effect immediately.
          </p>
        </div>

        {flagsError && (
          <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
            {flagsError}{' '}
            <button onClick={() => setFlagsError(null)} className="ml-2 font-bold">
              &times;
            </button>
          </div>
        )}

        {flagsLoading ? (
          <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
        ) : featureFlags.length === 0 ? (
          <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
            No feature flags available.
          </div>
        ) : (
          <div className="space-y-3">
            {featureFlags.map((flag) => {
              const isToggling = togglingKey === flag.key;
              const isOn = !!flag.value;
              const isDefault = flag.value === flag.default_value;
              return (
                <div
                  key={flag.key}
                  className="flex items-center justify-between rounded-lg border border-gray-100 px-4 py-3"
                >
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-gray-900">
                        {flag.key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                      </span>
                      {!isDefault && (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                          Modified
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[11px] text-gray-500">{flag.description}</p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isOn}
                    disabled={isToggling}
                    onClick={() => onToggleFlag(flag)}
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-40 ${
                      isOn ? 'bg-gray-900' : 'bg-gray-200'
                    }`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                        isOn ? 'translate-x-5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Signup Policy */}
      <div className="rounded-xl border border-gray-200 bg-white p-5">
        <div className="mb-3">
          <h2 className="text-[14px] font-semibold text-gray-900">Signup Policy</h2>
          <p className="mt-1 text-[12px] text-gray-500">
            Signups from listed domains auto-approve. Other domains require admin approval. Empty
            list = all signups auto-approve.
          </p>
        </div>

        {/* Add form */}
        <div className="mb-4 flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                setInputError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !adding) {
                  e.preventDefault();
                  onAdd();
                }
              }}
              placeholder="example.com or *.example.com"
              className="flex-1 rounded-md border border-gray-300 px-3 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
              disabled={adding}
              aria-label="Domain to add"
            />
            <button
              type="button"
              onClick={onAdd}
              disabled={adding}
              className="rounded-md bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
            >
              {adding ? 'Adding...' : 'Add'}
            </button>
          </div>
          <p className="text-[11px] text-gray-400">
            Enter <code className="rounded bg-gray-100 px-1">example.com</code> for an exact match
            or <code className="rounded bg-gray-100 px-1">*.example.com</code> to allow any
            subdomain. <code className="rounded bg-gray-100 px-1">*.example.com</code> does not
            match the bare suffix.
          </p>
          {inputError && (
            <p className="text-[12px] text-red-600" role="alert">
              {inputError}
            </p>
          )}
        </div>

        {/* Status */}
        {error && (
          <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
            {error}{' '}
            <button onClick={() => setError(null)} className="ml-2 font-bold">
              &times;
            </button>
          </div>
        )}
        {toast && (
          <div className="mb-3 rounded-lg bg-gray-900 px-3 py-2 text-[12px] text-white">
            {toast}
          </div>
        )}

        {/* List */}
        {loading ? (
          <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
        ) : domains.length === 0 ? (
          <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
            No allowed domains. All signups auto-approve.
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border border-gray-200">
            <table className="w-full text-[13px]">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Domain</th>
                  <th className="px-3 py-2 text-left font-medium">Type</th>
                  <th className="px-3 py-2 text-left font-medium">Added</th>
                  <th className="px-3 py-2 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {domains.map((d) => {
                  const key = `${d.domain}|${d.is_wildcard}`;
                  const isRemoving = removing === key;
                  return (
                    <tr key={key} className="bg-white">
                      <td className="px-3 py-2 text-gray-900">
                        {d.is_wildcard ? <span>*.{d.domain}</span> : d.domain}
                      </td>
                      <td className="px-3 py-2 text-gray-500">
                        {d.is_wildcard ? 'Wildcard' : 'Exact'}
                      </td>
                      <td className="px-3 py-2 text-gray-500">{relTime(d.created_at)}</td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          onClick={() => setConfirm(d)}
                          disabled={isRemoving}
                          className="text-[12px] font-medium text-red-600 transition hover:text-red-800 disabled:opacity-40"
                        >
                          {isRemoving ? 'Removing...' : 'Remove'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Confirm dialog */}
      {confirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
          onClick={() => setConfirm(null)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-[15px] font-semibold text-gray-900">Remove allowed domain</h3>
            <p className="mt-2 text-[13px] text-gray-600">
              Future signups from{' '}
              <code className="rounded bg-gray-100 px-1">
                {confirm.is_wildcard ? `*.${confirm.domain}` : confirm.domain}
              </code>{' '}
              will require admin approval. Existing users keep their current status.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirm(null)}
                className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => onRemove(confirm)}
                className="rounded-md bg-red-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-red-700"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
