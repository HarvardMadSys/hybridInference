'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT, type Translate } from '@/components/providers/useT';
import { setTwoFactorChallenger, type TwoFactorChallenge } from '@/lib/api/client';
import { TwoFactorCodeInput, useTwoFactorCode } from './TwoFactorCode';

function whyACodeIsOwed(t: Translate, reason: string): string {
  if (reason === 'environment_changed') {
    return t(
      'two_factor.prompt.environment_changed',
      'This session was verified in a different browser or app. Enter a code to keep using it here.',
    );
  }
  if (reason === 'stale') {
    return t(
      'two_factor.prompt.stale',
      'This change needs a code from the last few minutes. Enter one to continue.',
    );
  }
  return t('two_factor.prompt.unverified', 'Enter a code from your authenticator app to continue.');
}

/**
 * The console's answer to a request refused for want of an authenticator
 * code: a dialog that asks for one. Mounted once, above every page. The
 * request waits while it is open and is sent again once a code is accepted;
 * cancelling hands the refusal back to whatever made the request.
 *
 * Requests refused together (a page loading several things) share the one
 * dialog and its outcome.
 */
export function TwoFactorPrompt() {
  const t = useT();
  const { state } = useAuth();
  const [challenge, setChallenge] = useState<TwoFactorChallenge | null>(null);
  const waiting = useRef<{
    promise: Promise<boolean>;
    resolve: (verified: boolean) => void;
  } | null>(null);

  const settle = useCallback((verified: boolean) => {
    waiting.current?.resolve(verified);
    waiting.current = null;
    setChallenge(null);
  }, []);

  useEffect(() => {
    setTwoFactorChallenger((next) => {
      if (waiting.current) return waiting.current.promise;
      let resolve: (verified: boolean) => void = () => {};
      const promise = new Promise<boolean>((done) => {
        resolve = done;
      });
      waiting.current = { promise, resolve };
      setChallenge(next);
      return promise;
    });
    return () => {
      setTwoFactorChallenger(null);
      settle(false);
    };
  }, [settle]);

  // A code is asked of the session that was refused, not of whoever comes next.
  const signedIn = state.isAuthenticated;
  useEffect(() => {
    if (!signedIn) settle(false);
  }, [signedIn, settle]);

  return challenge ? (
    <CodeDialog reason={whyACodeIsOwed(t, challenge.reason)} onSettled={settle} />
  ) : null;
}

function CodeDialog({
  reason,
  onSettled,
}: {
  reason: string;
  onSettled: (verified: boolean) => void;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const { code, setCode, busy, error, complete, give } = useTwoFactorCode();
  const confirm = async () => {
    if (await give()) onSettled(true);
  };
  return (
    <ConfirmDialog
      title={t('two_factor.prompt.title', 'Two-factor authentication')}
      confirmLabel={t('two_factor.verify', 'Verify')}
      busyLabel={t('two_factor.verifying', 'Verifying…')}
      cancelLabel={t('two_factor.cancel', 'Cancel')}
      busy={busy}
      error={error}
      confirmDisabled={!complete}
      initialFocusRef={inputRef}
      onConfirm={() => void confirm()}
      onCancel={() => onSettled(false)}
    >
      <p>{reason}</p>
      <TwoFactorCodeInput
        value={code}
        onChange={setCode}
        onEnter={() => void confirm()}
        disabled={busy}
        inputRef={inputRef}
      />
    </ConfirmDialog>
  );
}
