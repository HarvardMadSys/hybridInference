'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { redeemAgentInvite, type AgentInviteRedeemResponse } from '@/lib/api/agentInvite';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { AuthField, AuthNotice } from '@/components/auth/AuthForm';
import { PasswordInput, PasswordRules } from '@/components/auth/PasswordInput';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';

function AgentInviteContent() {
  const t = useT();
  const branding = useBranding();
  const skin = useAuthAppearance();
  const searchParams = useSearchParams();
  // The invitation mail carries the token in the fragment, which browsers
  // never send to a server; the query form is accepted for hand-built links.
  const fragmentToken =
    typeof window !== 'undefined'
      ? new URLSearchParams(window.location.hash.replace(/^#/, '')).get('token')
      : null;
  const token = searchParams.get('token') ?? fragmentToken ?? '';
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AgentInviteRedeemResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError(t('auth.agent_invite.password_mismatch', 'The passwords do not match.'));
      return;
    }
    if (!accepted) {
      setError(
        t('auth.agent_invite.tos_required', 'You must agree to the Terms of Service to continue.'),
      );
      return;
    }
    setIsLoading(true);
    try {
      setResult(await redeemAgentInvite({ token, password, acceptedTos: true }));
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsLoading(false);
    }
  };

  if (result) {
    return (
      <AuthPageFrame
        page="signup"
        kicker={t('auth.agent_invite.kicker_done', 'ACCOUNT READY')}
        title={t('auth.agent_invite.done_title', 'Your account is ready')}
        subtitle={t(
          'auth.agent_invite.done_subtitle',
          'You are signed in. Save your agent API key now — it is shown only once.',
        )}
      >
        <div className={skin.form} data-auth="form">
          <AuthNotice tone="ok">
            {result.account_created
              ? t(
                  'auth.agent_invite.created',
                  'Your account was created with the password you set.',
                )
              : t(
                  'auth.agent_invite.updated',
                  'Your password was updated and your earlier sessions were signed out.',
                )}
          </AuthNotice>
          <AuthField
            id="apiKey"
            label={t('auth.agent_invite.key_label', 'Agent API key')}
            hint={fill(
              t(
                'auth.agent_invite.key_hint',
                'Paste this key into Cloud Agent under My settings → Model connections ({app_name}). It can use the agent models only, with this account’s usual quota.',
              ),
              { app_name: branding.appName },
            )}
          >
            <input
              id="apiKey"
              className={skin.input}
              data-auth="control"
              type="text"
              readOnly
              value={result.api_key}
              onFocus={(event) => event.target.select()}
            />
          </AuthField>
          <button
            type="button"
            className={skin.submit}
            data-auth="submit"
            onClick={async () => {
              await navigator.clipboard.writeText(result.api_key);
              setCopied(true);
            }}
          >
            {copied
              ? t('auth.agent_invite.copied', 'Copied')
              : t('auth.agent_invite.copy', 'Copy API key')}
          </button>
          <Link href="/dashboard" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.agent_invite.to_dashboard', 'Go to dashboard')}
          </Link>
        </div>
      </AuthPageFrame>
    );
  }

  return (
    <AuthPageFrame
      page="signup"
      kicker={t('auth.agent_invite.kicker', 'CLOUD AGENT INVITATION')}
      title={t('auth.agent_invite.title', 'Set your password')}
      subtitle={t(
        'auth.agent_invite.subtitle',
        'Your Cloud Agent account was approved. Set a password for this service to finish connecting it.',
      )}
      legal={
        <>
          {t('auth.legal.see', 'See')}{' '}
          <Link href="/terms">{t('chrome.footer.terms', 'Terms')}</Link>
          {' · '}
          <Link href="/terms#terms-s5">{t('chrome.footer.privacy', 'Privacy')}</Link>
        </>
      }
    >
      {!token ? (
        <div className={skin.form} data-auth="form">
          <AuthNotice tone="error">
            {t(
              'auth.agent_invite.missing_token',
              'This invitation link is incomplete. Please use the link from your invitation email.',
            )}
          </AuthNotice>
        </div>
      ) : (
        <form onSubmit={onSubmit} className={skin.form} data-auth="form">
          {error && <AuthNotice tone="error">{error}</AuthNotice>}
          <AuthField
            id="password"
            label={t('auth.signup.password_label', 'Password')}
            hint={
              <PasswordRules
                value={password}
                hint={t(
                  'auth.signup.password_hint',
                  'At least 8 characters with uppercase, lowercase, and numbers.',
                )}
              />
            }
          >
            <PasswordInput
              id="password"
              className={skin.input}
              data-auth="control"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </AuthField>
          <AuthField
            id="confirmPassword"
            label={t('auth.signup.confirm_password_label', 'Confirm Password')}
          >
            <PasswordInput
              id="confirmPassword"
              className={skin.input}
              data-auth="control"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </AuthField>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={accepted}
              onChange={(event) => setAccepted(event.target.checked)}
            />
            <span>
              {t('auth.agent_invite.accept_prefix', 'I agree to the')}{' '}
              <Link href="/terms">{t('chrome.footer.terms', 'Terms')}</Link>
            </span>
          </label>
          <button
            type="submit"
            className={skin.submit}
            data-auth="submit"
            disabled={isLoading}
            aria-busy={isLoading}
          >
            {isLoading
              ? t('auth.agent_invite.submitting', 'Setting your password…')
              : t('auth.agent_invite.submit', 'Set password and sign in')}
          </button>
        </form>
      )}
    </AuthPageFrame>
  );
}

export default function AgentInvitePage() {
  return (
    <Suspense>
      <AgentInviteContent />
    </Suspense>
  );
}
