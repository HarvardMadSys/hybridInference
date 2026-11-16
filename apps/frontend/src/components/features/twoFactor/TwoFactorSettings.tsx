'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { useAuth } from '@/components/providers';
import { useTwoFactor } from '@/components/providers/TwoFactorProvider';
import { useT } from '@/components/providers/useT';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import {
  confirmTwoFactor,
  isCompleteTwoFactorCode,
  newRecoveryCodes,
  removeTwoFactor,
  startTwoFactorSetup,
  type TwoFactorSetup,
} from '@/lib/api/twoFactor';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { TwoFactorCodeInput } from './TwoFactorCode';

/** The settings page's anchor for this section: `/dashboard/settings#two-factor`. */
export const TWO_FACTOR_ANCHOR = 'two-factor';

/** Few enough recovery codes left that the account should make a new set. */
const FEW_RECOVERY_CODES = 3;

function useAnchored(): boolean {
  const [anchored, setAnchored] = useState(false);
  useEffect(() => {
    const read = () => setAnchored(window.location.hash === `#${TWO_FACTOR_ANCHOR}`);
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);
  return anchored;
}

/**
 * Whether the settings page shows the two-factor section.
 *
 * The code is asked for before administrator access and nowhere else, so the
 * section is an administrator's. Any other account sees it once it has an
 * authenticator, or when it follows the section's link: an account must add
 * one before it can be made an administrator.
 */
export function useShowTwoFactorSettings(): boolean {
  const { state } = useAuth();
  const { status } = useTwoFactor();
  const anchored = useAnchored();
  if (!status) return false;
  return Boolean(state.user?.is_admin) || status.enabled || status.pending || anchored;
}

function ErrorText({ children }: { children: string | null }) {
  return children ? (
    <div
      role="alert"
      className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
    >
      {children}
    </div>
  ) : null;
}

/** The body of the settings page's two-factor section; the page draws the section around it. */
export function TwoFactorSettings() {
  const t = useT();
  const { state } = useAuth();
  const { status, refresh } = useTwoFactor();
  const [setup, setSetup] = useState<TwoFactorSetup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState<'setup' | 'codes' | 'remove' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);

  if (!status) return null;
  const { enabled, required, recovery_codes_left: codesLeft } = status;
  const hasPassword = state.user?.has_password !== false;

  const begin = async (event?: FormEvent) => {
    event?.preventDefault();
    if (busy) return;
    setBusy('setup');
    setError(null);
    try {
      // A first authenticator is confirmed by the password; replacing one by a
      // recent code, which the console asks for when the gateway wants it.
      setSetup(await startTwoFactorSetup(enabled ? undefined : password));
      setPassword('');
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const makeNewCodes = async () => {
    if (busy) return;
    setBusy('codes');
    setError(null);
    try {
      setCodes((await newRecoveryCodes()).recovery_codes);
      await refresh();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (busy) return;
    setBusy('remove');
    setError(null);
    try {
      await removeTwoFactor();
      setRemoving(false);
      await refresh();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  if (codes) {
    return <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />;
  }

  if (setup) {
    return (
      <ConfirmAuthenticator
        setup={setup}
        onCancel={() => setSetup(null)}
        onConfirmed={async (recoveryCodes) => {
          setSetup(null);
          setCodes(recoveryCodes);
          await refresh();
        }}
      />
    );
  }

  if (enabled) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-gray-50/70 px-4 py-3">
          <span className="text-sm font-medium text-gray-900">
            {fill(
              t('two_factor.settings.on', 'Authenticator app added. Recovery codes left: {count}'),
              { count: String(codesLeft) },
            )}
          </span>
          <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
            {t('two_factor.settings.on_badge', 'On')}
          </span>
        </div>
        {codesLeft < FEW_RECOVERY_CODES && (
          <p className="text-sm text-amber-700">
            {t(
              'two_factor.settings.few_codes',
              'Few recovery codes are left. Make a new set, so that losing your device does not lock you out.',
            )}
          </p>
        )}
        {!removing && <ErrorText>{error}</ErrorText>}
        <div className="flex flex-wrap justify-end gap-2">
          {!required && (
            <Button
              type="button"
              variant="subtle"
              disabled={busy !== null}
              onClick={() => {
                setError(null);
                setRemoving(true);
              }}
            >
              {t('two_factor.settings.remove', 'Turn off')}
            </Button>
          )}
          <Button
            type="button"
            variant="secondary"
            isLoading={busy === 'codes'}
            disabled={busy !== null}
            onClick={() => void makeNewCodes()}
          >
            {t('two_factor.settings.new_codes', 'New recovery codes')}
          </Button>
          <Button
            type="button"
            variant="secondary"
            isLoading={busy === 'setup'}
            disabled={busy !== null}
            onClick={() => void begin()}
          >
            {t('two_factor.settings.replace', 'Change authenticator app')}
          </Button>
        </div>
        {removing && (
          <ConfirmDialog
            title={t('two_factor.remove.title', 'Turn off two-factor authentication?')}
            confirmLabel={t('two_factor.remove.confirm', 'Turn off')}
            busyLabel={t('two_factor.remove.busy', 'Turning off…')}
            cancelLabel={t('two_factor.remove.keep', 'Keep it on')}
            tone="danger"
            busy={busy === 'remove'}
            error={error}
            onConfirm={() => void remove()}
            onCancel={() => {
              setRemoving(false);
              setError(null);
            }}
          >
            <p>
              {t(
                'two_factor.remove.body',
                'The authenticator app and every recovery code stop working for this account.',
              )}
            </p>
          </ConfirmDialog>
        )}
      </div>
    );
  }

  if (!hasPassword) {
    return (
      <p className="text-sm text-gray-600">
        {t(
          'two_factor.settings.needs_password',
          'Adding an authenticator app is confirmed with your password, and this account has none. Set a password first.',
        )}
      </p>
    );
  }

  return (
    <form onSubmit={begin} className="space-y-4">
      {required && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {t(
            'two_factor.settings.required',
            'Administrator access is off for this account until it has an authenticator app.',
          )}
        </div>
      )}
      <ErrorText>{error}</ErrorText>
      <InputField
        label={t('two_factor.settings.password', 'Your password')}
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <div className="flex justify-end">
        <Button type="submit" isLoading={busy === 'setup'} disabled={!password}>
          {t('two_factor.settings.begin', 'Add authenticator app')}
        </Button>
      </div>
    </form>
  );
}

/** The new secret, as a QR code and as text, and the first code that turns it on. */
function ConfirmAuthenticator({
  setup,
  onCancel,
  onConfirmed,
}: {
  setup: TwoFactorSetup;
  onCancel: () => void;
  onConfirmed: (recoveryCodes: string[]) => Promise<void>;
}) {
  const t = useT();
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !isCompleteTwoFactorCode(code)) return;
    setBusy(true);
    setError(null);
    try {
      const { recovery_codes: recoveryCodes } = await confirmTwoFactor(code.trim());
      await onConfirmed(recoveryCodes);
    } catch (e) {
      setError(getErrorMessage(e));
      setBusy(false);
    }
  };

  return (
    <form onSubmit={confirm} className="space-y-4">
      <p className="text-sm text-gray-700">
        {t(
          'two_factor.setup.scan',
          'Scan this code with an authenticator app, such as Duo Mobile, Google Authenticator or 1Password. Then enter the six digits the app shows.',
        )}
      </p>
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
        {/* Black on white whatever the console's theme: a scanner reads contrast. */}
        <div className="rounded-lg border border-gray-200 bg-white p-3">
          <QRCodeSVG
            value={setup.otpauth_uri}
            size={168}
            marginSize={1}
            title={t('two_factor.setup.qr_title', 'QR code for your authenticator app')}
          />
        </div>
        <div className="min-w-0 text-sm text-gray-600">
          <p>{t('two_factor.setup.manual', 'Cannot scan it? Enter this key in the app:')}</p>
          <code className="mt-1.5 block break-all rounded-md bg-gray-100 px-2.5 py-1.5 font-mono text-[13px] text-gray-900">
            {setup.secret}
          </code>
        </div>
      </div>
      <ErrorText>{error}</ErrorText>
      <TwoFactorCodeInput value={code} onChange={setCode} disabled={busy} />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="subtle" disabled={busy} onClick={onCancel}>
          {t('two_factor.cancel', 'Cancel')}
        </Button>
        <Button type="submit" isLoading={busy} disabled={!isCompleteTwoFactorCode(code)}>
          {t('two_factor.setup.confirm', 'Turn on')}
        </Button>
      </div>
    </form>
  );
}

/** A new set of recovery codes, shown this once. */
function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  // A plain-http console (a LAN address) has no async clipboard.
  const canCopy = typeof window !== 'undefined' && window.isSecureContext && !!navigator.clipboard;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(codes.join('\n'));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        {t(
          'two_factor.codes.keep',
          'Save these recovery codes somewhere safe, away from your phone. Each one works once in place of a code from the app, and they are not shown again.',
        )}
      </div>
      <ul
        aria-label={t('two_factor.codes.label', 'Recovery codes')}
        className="grid grid-cols-2 gap-x-6 gap-y-1.5 rounded-lg border border-gray-200 bg-gray-50/70 px-4 py-3 font-mono text-sm text-gray-900"
      >
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
      <div className="flex justify-end gap-2">
        {canCopy && (
          <Button type="button" variant="secondary" onClick={() => void copy()}>
            {copied ? t('two_factor.codes.copied', 'Copied') : t('two_factor.codes.copy', 'Copy')}
          </Button>
        )}
        <Button type="button" onClick={onDone}>
          {t('two_factor.codes.done', 'I have saved them')}
        </Button>
      </div>
    </div>
  );
}
