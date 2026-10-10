'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import type { ConfigResponse } from '@/lib/api/config';
import { getSetupStatus } from '@/lib/api/setup';
import { getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';

import { ConfigureStep } from './ConfigureStep';
import { CreateAdminStep } from './CreateAdminStep';
import { FinishStep } from './FinishStep';

type Stage =
  | { kind: 'checking' }
  | { kind: 'check-failed'; message: string }
  | { kind: 'not-required' }
  | { kind: 'create' }
  | { kind: 'configure' }
  | { kind: 'finish'; config: ConfigResponse };

const STEPS = [
  { kind: 'create', label: 'Create administrator' },
  { kind: 'configure', label: 'Configure' },
  { kind: 'finish', label: 'Finish' },
] as const;

const SUBTITLES: Record<(typeof STEPS)[number]['kind'], string> = {
  create: 'Create the first administrator. Only someone who can read the backend log can do this.',
  configure: 'Fill in what this deployment needs now. Everything else can wait.',
  finish: 'Almost done.',
};

function Spinner({ label }: { label: string }) {
  return (
    <div className="flex justify-center py-16" role="status" aria-live="polite">
      <div
        className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"
        aria-hidden
      />
      <span className="sr-only">{label}</span>
    </div>
  );
}

/**
 * `/setup`: first-run setup, in three steps.
 *
 * Whether setup is still pending is asked of the backend when the page opens —
 * `GET /auth/setup/status` is the authority, not the site configuration this
 * page was rendered with — and not again: once step 1 creates the
 * administrator the answer becomes "no", and the remaining steps carry on
 * signed in as that administrator.
 */
export default function SetupPage() {
  const router = useRouter();
  const { branding, setup } = useSiteConfig();
  // The server-rendered configuration also says setup is pending, so the gate
  // is active and a client-side navigation away would come straight back.
  const gateActive = setup?.required === true;
  const [stage, setStage] = useState<Stage>({ kind: 'checking' });

  const check = useCallback(async () => {
    setStage({ kind: 'checking' });
    try {
      const status = await getSetupStatus();
      setStage({ kind: status.setup_required ? 'create' : 'not-required' });
    } catch (error) {
      setStage({ kind: 'check-failed', message: getErrorMessage(error) });
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  useEffect(() => {
    if (stage.kind === 'not-required' && !gateActive) router.replace('/dashboard');
  }, [stage.kind, gateActive, router]);

  if (stage.kind === 'checking') return <Spinner label="Checking setup status…" />;

  if (stage.kind === 'not-required') {
    if (!gateActive) return <Spinner label="Opening the dashboard…" />;
    // The backend says setup is done while this page's configuration still
    // says it is pending. Leave on a full load, and only when asked: an
    // automatic redirect could bounce between the two answers.
    return (
      <div className="mx-auto w-full max-w-md">
        <Card>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Already set up</h1>
          <p className="mt-2 text-sm text-gray-600">
            This deployment already has an administrator.
          </p>
          <Button type="button" className="mt-6" onClick={() => navigateTo('/dashboard')}>
            Continue
          </Button>
        </Card>
      </div>
    );
  }

  if (stage.kind === 'check-failed') {
    return (
      <div className="mx-auto w-full max-w-md">
        <Card>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Setup</h1>
          <p className="mt-2 text-sm text-red-700" role="alert">
            Could not reach the backend to check whether setup is pending: {stage.message}
          </p>
          <Button type="button" variant="secondary" className="mt-6" onClick={() => void check()}>
            Try again
          </Button>
        </Card>
      </div>
    );
  }

  const current = STEPS.findIndex((step) => step.kind === stage.kind);

  return (
    <div className="mx-auto w-full max-w-2xl">
      <Card>
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">
          Set up {branding.appName}
        </h1>
        <p className="mt-2 text-sm text-gray-600">{SUBTITLES[stage.kind]}</p>

        <ol className="mt-6 flex flex-wrap gap-x-6 gap-y-2 text-[13px]" aria-label="Setup steps">
          {STEPS.map((step, index) => {
            const state = index < current ? 'done' : index === current ? 'current' : 'upcoming';
            return (
              <li
                key={step.kind}
                aria-current={state === 'current' ? 'step' : undefined}
                className={`flex items-center gap-2 ${
                  state === 'current'
                    ? 'font-semibold text-gray-900'
                    : state === 'done'
                      ? 'text-gray-500'
                      : 'text-gray-400'
                }`}
              >
                <span
                  className={`inline-flex h-6 w-6 items-center justify-center rounded-full text-[12px] ${
                    state === 'upcoming' ? 'bg-gray-100 text-gray-500' : 'bg-gray-900 text-white'
                  }`}
                  aria-hidden
                >
                  {state === 'done' ? '✓' : index + 1}
                </span>
                {step.label}
                {state === 'done' ? <span className="sr-only">(done)</span> : null}
              </li>
            );
          })}
        </ol>

        <div className="mt-8">
          {stage.kind === 'create' ? (
            <CreateAdminStep onCreated={() => setStage({ kind: 'configure' })} />
          ) : stage.kind === 'configure' ? (
            <ConfigureStep onSaved={(config) => setStage({ kind: 'finish', config })} />
          ) : (
            <FinishStep config={stage.config} />
          )}
        </div>
      </Card>
    </div>
  );
}
