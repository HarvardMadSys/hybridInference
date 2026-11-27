'use client';

import { useState } from 'react';
import { createServiceAccount, type CreateServiceAccountResponse } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

/** Create a service account: a key-only account with no browser sign-in. */
export function ServiceAccountCreate({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [loginName, setLoginName] = useState('');
  const [role, setRole] = useState<'free' | 'pro' | 'internal'>('free');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreateServiceAccountResponse | null>(null);
  const [copied, setCopied] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await createServiceAccount({
        name: name.trim(),
        login_name: loginName.trim() || undefined,
        role,
        password: password || undefined,
      });
      setCreated(result);
      onCreated();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Create service account">
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
        {created ? (
          <>
            <h2 className="text-[15px] font-semibold text-gray-900">Service account created</h2>
            <p className="mt-1 text-[13px] text-gray-600">
              {created.name} (<span className="font-mono">{created.login_name}</span>) — save this
              API key now; it is shown only once.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded bg-gray-100 px-2 py-1.5 font-mono text-[12px]">{created.api_key}</code>
              <button
                type="button"
                className="shrink-0 rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700"
                onClick={() => {
                  void navigator.clipboard.writeText(created.api_key);
                  setCopied(true);
                }}
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div className="mt-4 flex justify-end">
              <button type="button" className="rounded-md border border-gray-300 px-3 py-1.5 text-[13px] hover:bg-gray-50" onClick={onClose}>
                Done
              </button>
            </div>
          </>
        ) : (
          <form onSubmit={submit}>
            <h2 className="text-[15px] font-semibold text-gray-900">New service account</h2>
            <p className="mt-1 text-[13px] text-gray-600">
              A key-only account: no email, no browser sign-in. Its first API key is issued here
              and shown once.
            </p>
            <label className="mt-4 block text-[13px] font-medium text-gray-700">
              Name
              <input required value={name} onChange={(e) => setName(e.target.value)} placeholder="Nightly eval bot" className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-[13px]" />
            </label>
            <label className="mt-3 block text-[13px] font-medium text-gray-700">
              Login name <span className="font-normal text-gray-400">(optional, derived from name)</span>
              <input value={loginName} onChange={(e) => setLoginName(e.target.value)} placeholder="nightly-eval-bot" className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 font-mono text-[13px]" />
            </label>
            <label className="mt-3 block text-[13px] font-medium text-gray-700">
              Role
              <select value={role} onChange={(e) => setRole(e.target.value as typeof role)} className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-[13px]">
                <option value="free">free</option>
                <option value="pro">pro</option>
                <option value="internal">internal</option>
              </select>
            </label>
            <label className="mt-3 block text-[13px] font-medium text-gray-700">
              Your admin password
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-[13px]" />
            </label>
            {error && <div className="mt-3 rounded-md border border-red-200 bg-red-50 p-2 text-[13px] text-red-700">{error}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="rounded-md border border-gray-300 px-3 py-1.5 text-[13px] hover:bg-gray-50" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" disabled={busy || !name.trim()} className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-50">
                {busy ? 'Creating…' : 'Create account + key'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
