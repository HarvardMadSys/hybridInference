'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  SignupAllowedDomain,
  addSignupAllowedDomain,
  listSignupAllowedDomains,
  removeSignupAllowedDomain,
} from '@/lib/api/admin';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_GHOST,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CONTROL,
  PANEL,
  ROW_ACTION_DANGER,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import type { ConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { getErrorMessage } from '@/lib/utils/errors';

import { validateSignupDomainInput } from './signupDomainValidation';

/** The write key of Add domain, so only its button reads "Adding…". */
const ADD_DOMAIN = 'add domain';

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

/**
 * The signup allowlist: signups from a listed domain are approved at once.
 *
 * `confirmation` is the page's, shared with its other confirmed writes, so
 * only one of them is ever asked for at a time.
 */
export function SignupPolicySection({
  id,
  confirmation,
  onToast,
}: {
  /** Anchor of the section. */
  id?: string;
  confirmation: ConfirmAction;
  onToast: (msg: string) => void;
}) {
  const addingNow = confirmation.savingKey === ADD_DOMAIN;
  const [domains, setDomains] = useState<SignupAllowedDomain[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [addingDomain, setAddingDomain] = useState(false);
  const [input, setInput] = useState('');
  const [inputError, setInputError] = useState<string | null>(null);

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

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  // A domain the administrator typed is added at once; a failure stays under the input.
  const onAddDomain = async () => {
    setInputError(null);
    const validated = validateSignupDomainInput(input);
    if (!validated.ok) {
      setInputError(validated.error);
      return;
    }
    const failure = await confirmation.perform({
      key: ADD_DOMAIN,
      run: async () => {
        await addSignupAllowedDomain(input.trim());
        setInput('');
        setAddingDomain(false);
        onToast(
          validated.isWildcard
            ? `Added wildcard *.${validated.domain}.`
            : `Added ${validated.domain}.`,
        );
        await loadDomains();
      },
    });
    if (failure) setInputError(failure);
  };

  const onRemove = (row: SignupAllowedDomain) => {
    const shownDomain = row.is_wildcard ? `*.${row.domain}` : row.domain;
    // The last domain empties the list, and an empty list approves every signup.
    const last = domains.length === 1;
    confirmation.request({
      title: `Remove ${shownDomain} from the signup allowlist?`,
      confirmLabel: 'Remove',
      busyLabel: 'Removing…',
      tone: 'danger',
      body: (
        <p className="[overflow-wrap:anywhere]">
          {last ? (
            <>
              It is the last allowed domain. With the list empty, every signup is approved
              automatically.
            </>
          ) : (
            <>
              Future signups from <code className="rounded bg-gray-100 px-1">{shownDomain}</code>{' '}
              will require admin approval.
            </>
          )}{' '}
          Existing users keep their current status.
        </p>
      ),
      run: async () => {
        await removeSignupAllowedDomain(row.domain, row.is_wildcard);
        onToast(row.is_wildcard ? `Removed wildcard *.${row.domain}.` : `Removed ${row.domain}.`);
        await loadDomains();
      },
    });
  };

  return (
    <AdminSection
      id={id}
      title="Signup Policy"
      description="Signups from listed domains auto-approve. Other domains require admin approval. Empty list = all signups auto-approve."
      actions={
        addingDomain ? null : (
          <button type="button" onClick={() => setAddingDomain(true)} className={BUTTON_SECONDARY}>
            Add domain
          </button>
        )
      }
    >
      {/* Add form: opened explicitly; its Add domain writes the domain. */}
      {addingDomain ? (
        <form
          className="mb-4 flex max-w-xl flex-col gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!confirmation.busy) void onAddDomain();
          }}
        >
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                setInputError(null);
              }}
              placeholder="example.com or *.example.com"
              className={`${CONTROL} min-w-[12rem] flex-1`}
              disabled={confirmation.busy}
              aria-label="Domain to add"
              autoFocus
            />
            <button
              type="button"
              onClick={() => {
                setAddingDomain(false);
                setInput('');
                setInputError(null);
              }}
              disabled={confirmation.busy}
              className={BUTTON_GHOST}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={confirmation.busy || !input.trim()}
              aria-busy={addingNow || undefined}
              className={BUTTON_PRIMARY}
            >
              {addingNow ? 'Adding…' : 'Add domain'}
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
        </form>
      ) : null}

      {error && (
        <div className="mb-3">
          <AdminNotice onDismiss={() => setError(null)}>{error}</AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : domains.length === 0 ? (
        <AdminEmptyState title="No allowed domains. All signups auto-approve." />
      ) : (
        <div className={`${PANEL} relative overflow-x-auto`}>
          <table className="w-full min-w-[30rem] text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Domain</th>
                <th className={TABLE_HEAD_CELL}>Type</th>
                <th className={TABLE_HEAD_CELL}>Added</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {domains.map((d) => {
                const key = `${d.domain}|${d.is_wildcard}`;
                return (
                  <tr key={key}>
                    <td className="px-4 py-2 font-mono text-[12px] text-gray-900">
                      {d.is_wildcard ? <span>*.{d.domain}</span> : d.domain}
                    </td>
                    <td className="px-4 py-2 text-gray-500">
                      {d.is_wildcard ? 'Wildcard' : 'Exact'}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-gray-500">
                      {relTime(d.created_at)}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => onRemove(d)}
                        disabled={confirmation.busy}
                        aria-label={`Remove ${d.is_wildcard ? `*.${d.domain}` : d.domain}`}
                        className={ROW_ACTION_DANGER}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </AdminSection>
  );
}
