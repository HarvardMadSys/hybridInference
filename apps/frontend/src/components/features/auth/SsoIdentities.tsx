'use client';

import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { ssoStartFailure } from '@/components/auth/ssoMessages';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT, type Translate } from '@/components/providers/useT';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import {
  linkSsoIdentity,
  listSsoIdentities,
  ssoRefusalMessage,
  unlinkSsoIdentity,
  type SsoIdentity,
} from '@/lib/api/sso';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { createAuthSchemas } from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { navigateTo } from '@/lib/utils/navigation';
import {
  SsoReauthConfirm,
  ssoReauthRequired,
  useSsoReauth,
  useSsoReturnPath,
  type SsoReauth,
} from './SsoReauth';

/** The signed-in account's identities, and whether they have been read yet. */
export interface SsoIdentitiesState {
  identities: SsoIdentity[];
  loaded: boolean;
  reload: () => Promise<void>;
}

/**
 * The identities that can sign in to the account `userId`.
 *
 * Read whether or not single sign-on is offered now: an identity linked while
 * it was stays on the account, and its owner must still be able to see it and
 * remove it. A gateway that cannot answer leaves the list as it was -- empty,
 * for one that predates single sign-on.
 */
export function useSsoIdentities(userId: string | null): SsoIdentitiesState {
  const [identities, setIdentities] = useState<SsoIdentity[]>([]);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    try {
      setIdentities(await listSsoIdentities());
    } catch {
      // Shown as it was; the section says nothing it has not read.
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (userId) void reload();
  }, [userId, reload]);

  return useMemo(() => ({ identities, loaded, reload }), [identities, loaded, reload]);
}

/**
 * How the owner confirms a change to the account's identities: its password,
 * or a sign-in at its identity provider when it has no password and no phone.
 * An account with a phone and no password has neither here -- a verification
 * code does not confirm these two actions -- and is told to set a password.
 */
type OwnerProof = { kind: 'password' } | { kind: 'sso'; reauth: SsoReauth } | { kind: 'none' };

const CONFIRM_BOX = 'mt-3 space-y-3 rounded-lg border border-gray-200 bg-gray-50/70 p-3';

/** Why the account's only way in stays: said before it is tried, and when it is refused. */
function lastMethod(t: Translate): string {
  return t(
    'settings.sso.last_method',
    'This is the only way to sign in to this account. Set a password before disconnecting it.',
  );
}

/**
 * The body of the settings page's "Single sign-on" section: the identities
 * that can sign in to this account, removing one, and linking another. The
 * page draws the section around it, and decides whether to draw it at all.
 */
export function SsoIdentitiesPanel({ identities, loaded, reload }: SsoIdentitiesState) {
  const t = useT();
  const format = useFormatter();
  const { state, refreshUser } = useAuth();
  const { sso } = useSiteConfig();
  const linkReauth = useSsoReauth('link_sso');
  const unlinkReauth = useSsoReauth('unlink_sso');
  const hasPassword = state.user?.has_password !== false;
  // Back from the identity provider, the action it was left for is open again:
  // the one this tab remembers leaving for, not one an address merely names.
  const [unlinking, setUnlinking] = useState<string | null>(() => unlinkReauth?.draft ?? null);
  const [connecting, setConnecting] = useState(() => linkReauth?.draft != null);

  const proofFor = (reauth: SsoReauth | null): OwnerProof =>
    hasPassword ? { kind: 'password' } : reauth ? { kind: 'sso', reauth } : { kind: 'none' };
  const unlinkProof = proofFor(unlinkReauth);
  const linkProof = proofFor(linkReauth);
  const canChange = unlinkProof.kind !== 'none';
  // An account confirms at its provider because it has no password and no
  // phone, so its one identity is the only way in and the gateway keeps it.
  // Said here, it spares a round trip to the provider that could only be refused.
  const onlyWayIn = unlinkProof.kind === 'sso' && identities.length === 1;

  // Removing an identity can change how the account confirms the next action.
  const changed = async () => {
    setUnlinking(null);
    await Promise.all([reload(), refreshUser()]);
  };

  return (
    <div className="space-y-4">
      {!loaded ? (
        <p className="text-sm text-gray-500">{t('common.loading', 'Loading...')}</p>
      ) : identities.length === 0 ? (
        <p className="text-sm text-gray-600">
          {t('settings.sso.empty', 'No organization account is connected.')}
        </p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {identities.map((identity) => (
            <li key={identity.id} className="py-3 first:pt-0 last:pb-0">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-gray-900">
                      {identity.provider_name}
                    </span>
                    {!identity.available && (
                      <span className="inline-flex items-center rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-600/20">
                        {t('settings.sso.unavailable_badge', 'No longer available')}
                      </span>
                    )}
                  </div>
                  {identity.email && (
                    <p className="mt-0.5 truncate text-sm text-gray-600">{identity.email}</p>
                  )}
                  <p className="mt-0.5 text-xs text-gray-500">
                    {fill(t('settings.sso.connected_on', 'Connected {date}'), {
                      date: format.date(identity.linked_at, {
                        year: 'numeric',
                        month: 'short',
                        day: 'numeric',
                      }),
                    })}
                  </p>
                </div>
                {canChange && !onlyWayIn && unlinking !== identity.id && (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className="shrink-0"
                    aria-label={`${t('settings.sso.disconnect', 'Disconnect')} ${identity.provider_name}`}
                    onClick={() => setUnlinking(identity.id)}
                  >
                    {t('settings.sso.disconnect', 'Disconnect')}
                  </Button>
                )}
              </div>
              {unlinkProof.kind !== 'none' && !onlyWayIn && unlinking === identity.id && (
                <DisconnectConfirm
                  identity={identity}
                  proof={unlinkProof}
                  onDone={changed}
                  onCancel={() => setUnlinking(null)}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {onlyWayIn && <p className="text-sm text-gray-600">{lastMethod(t)}</p>}

      {!canChange ? (
        <p className="text-sm text-gray-600">
          {t('settings.sso.needs_password', 'Set a password before changing single sign-on.')}
        </p>
      ) : !sso.enabled ? null : connecting && linkProof.kind !== 'none' ? (
        <ConnectForm proof={linkProof} onCancel={() => setConnecting(false)} />
      ) : (
        <div className="flex justify-end">
          <Button type="button" variant="secondary" onClick={() => setConnecting(true)}>
            {t('settings.sso.connect', 'Connect an SSO account')}
          </Button>
        </div>
      )}
    </div>
  );
}

/** The owner's password, where a change to the identities is confirmed with it. */
function OwnerPassword({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const t = useT();
  return (
    <InputField
      label={t('auth.email_bind.current_password', 'Current password')}
      type="password"
      autoComplete="current-password"
      hint={t('auth.email_bind.current_password_hint', 'Confirms that this change is yours.')}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      disabled={disabled}
    />
  );
}

function DisconnectConfirm({
  identity,
  proof,
  onDone,
  onCancel,
}: {
  identity: SsoIdentity;
  proof: Exclude<OwnerProof, { kind: 'none' }>;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const t = useT();
  const toast = useAccountToast();
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reauth = proof.kind === 'sso' ? proof.reauth : null;
  const ready = reauth ? reauth.confirmed : password !== '';

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setError(null);
    try {
      // Confirmed at the identity provider, the request carries no proof of its own.
      await unlinkSsoIdentity(identity.id, reauth ? {} : { password });
      reauth?.settle();
      toast.success(t('settings.sso.disconnected_toast', 'Single sign-on disconnected.'));
      await onDone();
    } catch (err) {
      reauth?.settle(err);
      setError(
        err instanceof APIError && err.statusCode === 409
          ? lastMethod(t)
          : (ssoRefusalMessage(err) ?? getErrorMessage(err)),
      );
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className={CONFIRM_BOX}>
      {reauth ? (
        // The identity being removed is what the page must remember across the trip.
        <SsoReauthConfirm reauth={reauth} draft={() => identity.id} disabled={busy} />
      ) : (
        <OwnerPassword value={password} onChange={setPassword} disabled={busy} />
      )}
      {error ? (
        <p className="text-xs text-red-600" role="alert">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="subtle" size="sm" onClick={onCancel} disabled={busy}>
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button
          type="submit"
          variant="danger"
          size="sm"
          disabled={busy || !ready}
          aria-busy={busy || undefined}
        >
          {t('settings.sso.disconnect', 'Disconnect')}
        </Button>
      </div>
    </form>
  );
}

function ConnectForm({
  proof,
  onCancel,
}: {
  proof: Exclude<OwnerProof, { kind: 'none' }>;
  onCancel: () => void;
}) {
  const t = useT();
  const returnTo = useSsoReturnPath();
  const emailSchema = useMemo(() => createAuthSchemas(t).emailSchema, [t]);
  const reauth = proof.kind === 'sso' ? proof.reauth : null;
  // The address typed before leaving for the identity provider comes back with it.
  const [email, setEmail] = useState(() => reauth?.draft ?? '');
  const [emailError, setEmailError] = useState<string | undefined>();
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const trimmed = email.trim();
    const parsed = emailSchema.safeParse(trimmed);
    if (!parsed.success) {
      setEmailError(parsed.error.issues[0]?.message);
      return;
    }
    setEmailError(undefined);
    if (reauth ? !reauth.confirmed : !password) {
      setError(
        reauth
          ? ssoReauthRequired(t, reauth.provider)
          : t(
              'auth.email_bind.password_required',
              'Enter your current password to confirm this change.',
            ),
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const url = await linkSsoIdentity({
        email: trimmed,
        returnTo,
        ...(reauth ? {} : { password }),
      });
      // The gateway takes the confirmation before it looks for a provider, so
      // it is used whichever answer comes back.
      reauth?.settle();
      if (url) {
        // The page is on its way out: the button stays busy until it is gone.
        navigateTo(url);
        return;
      }
      setError(
        t(
          'auth.sso.not_available',
          'Your organization has not set up single sign-on here. Use another sign-in method.',
        ),
      );
    } catch (err) {
      reauth?.settle(err);
      const status = err instanceof APIError ? err.statusCode : undefined;
      setError(ssoStartFailure(t, status) ?? ssoRefusalMessage(err) ?? getErrorMessage(err));
    }
    setBusy(false);
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <InputField
        label={t('auth.sso.email_label', 'Work or school email')}
        type="email"
        autoComplete="email"
        autoCapitalize="none"
        spellCheck={false}
        value={email}
        onChange={(event) => {
          setEmail(event.target.value);
          setEmailError(undefined);
          setError(null);
        }}
        error={emailError}
        disabled={busy}
      />
      {reauth ? (
        <SsoReauthConfirm reauth={reauth} draft={() => email} disabled={busy} />
      ) : (
        <OwnerPassword value={password} onChange={setPassword} disabled={busy} />
      )}
      {error ? (
        <div
          className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          {error}
        </div>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="subtle" onClick={onCancel} disabled={busy}>
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button type="submit" disabled={busy} aria-busy={busy || undefined}>
          {t('auth.sso.continue', 'Continue')}
        </Button>
      </div>
    </form>
  );
}
