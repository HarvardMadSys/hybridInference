'use client';

import { KeyList } from '@/components/features/configuration/KeyList';
import { CONFIGURATION_TAB_PATH } from '@/components/features/configuration/paths';
import {
  ManualRestartHint,
  RestartBackendButton,
} from '@/components/features/configuration/RestartBackend';
import { Button } from '@/components/ui/Button';
import type { ConfigResponse } from '@/lib/api/config';
import { navigateTo } from '@/lib/utils/navigation';

/**
 * Step 3: restart if anything saved needs it, then leave for the admin
 * Configuration tab.
 *
 * Both ways out are full page loads. The console read its site configuration
 * when this page loaded, while setup was still pending; only a fresh load reads
 * the configuration that says it is done.
 */
export function FinishStep({ config }: { config: ConfigResponse }) {
  const finish = () => navigateTo(CONFIGURATION_TAB_PATH);
  const restartPending = config.pending_restart.length > 0;

  return (
    <div className="space-y-5">
      <p className="text-sm text-gray-700">
        The administrator account is ready and you are signed in. Everything here can be changed
        later on the Configuration tab.
      </p>

      {config.missing.length > 0 ? (
        <div
          className="rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
          role="status"
        >
          <p>
            <span className="font-semibold">Still missing:</span> <KeyList keys={config.missing} />.
          </p>
          <p className="mt-1">
            Signed-in users see a warning until these have values, and features that need them do
            not work.
          </p>
        </div>
      ) : null}

      {restartPending ? (
        <section
          aria-labelledby="setup-restart-heading"
          className="rounded-lg border border-gray-200 p-4"
        >
          <h2 id="setup-restart-heading" className="text-sm font-semibold text-gray-900">
            Restart to apply
          </h2>
          <p className="mt-1 text-[13px] text-gray-600">
            {config.pending_restart.length === 1 ? 'This setting applies' : 'These settings apply'}{' '}
            after the backend restarts: <KeyList keys={config.pending_restart} />.
          </p>
          <div className="mt-3">
            {config.restart_supported ? (
              // The console's primary button, like the rest of this page.
              <RestartBackendButton
                onRestarted={finish}
                className="inline-flex h-10 items-center justify-center rounded-md bg-black px-4 text-sm font-medium text-white shadow-sm transition-colors duration-200 hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-black focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              />
            ) : (
              <ManualRestartHint />
            )}
          </div>
        </section>
      ) : null}

      <div className="flex justify-end">
        <Button
          type="button"
          variant={restartPending && config.restart_supported ? 'secondary' : 'primary'}
          onClick={finish}
        >
          Finish
        </Button>
      </div>
    </div>
  );
}
