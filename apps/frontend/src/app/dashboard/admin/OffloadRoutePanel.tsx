'use client';

import { useEffect, useState } from 'react';
import toast from 'react-hot-toast';

import type { OffloadRoute, ProviderRoute } from '@/lib/api/admin';
import { clearOffloadRoute, setOffloadRoute } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { validateNumericSettingInput } from './numericSettingValidation';

// Prefilled for a model that has no offload route yet: long enough that a brief
// burst is absorbed by the queue, short enough to be well inside the default
// 30s acquire timeout.
const DEFAULT_WAIT_SECONDS = '5';

export interface OffloadQueueInfo {
  enabled: boolean;
  maxWaitSeconds: number;
}

interface OffloadRoutePanelProps {
  modelId: string;
  routes: ProviderRoute[];
  offload: OffloadRoute | null;
  queue: OffloadQueueInfo | null;
  /** False for a model whose routing policy cannot use an offload route; only Clear is offered. */
  editable: boolean;
  loadError?: string | null;
  routeLabel: (route: ProviderRoute) => string;
  onChange: (modelId: string, offload: OffloadRoute | null) => void;
}

type ValidatedLimit = { ok: true; value: number | null } | { ok: false; error: string };

function storedLimitText(limit: number | null | undefined): string {
  return limit === null || limit === undefined ? '' : String(limit);
}

// Blank is no limit: for the engine queue limit every streaming request goes
// straight to its engine, and for the max input a request of any size may be
// offloaded.
function validateLimit(raw: string): ValidatedLimit {
  if (raw.trim() === '') return { ok: true, value: null };
  return validateNumericSettingInput(raw, { min: 1, max: null, integer: true });
}

// No upper bound: the gateway's queue ends a wait at its own acquire timeout either
// way, and a longer wait gives an engine longer to send its first token.
function validateWait(raw: string) {
  const validated = validateNumericSettingInput(raw, {
    min: null,
    max: null,
    integer: false,
  });
  if (validated.ok && validated.value <= 0) {
    return { ok: false as const, error: 'Must be greater than 0.' };
  }
  return validated;
}

export function OffloadRoutePanel({
  modelId,
  routes,
  offload,
  queue,
  editable,
  loadError,
  routeLabel,
  onChange,
}: OffloadRoutePanelProps) {
  const [draftRouteId, setDraftRouteId] = useState(offload?.route_id ?? '');
  const [draftWait, setDraftWait] = useState(
    offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS,
  );
  const [draftLimit, setDraftLimit] = useState(storedLimitText(offload?.engine_queue_limit));
  const [draftMaxInput, setDraftMaxInput] = useState(storedLimitText(offload?.max_input_tokens));
  const [saving, setSaving] = useState(false);

  // Start over whenever the model, or what is stored for it, changes.
  useEffect(() => {
    setDraftRouteId(offload?.route_id ?? '');
    setDraftWait(offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS);
    setDraftLimit(storedLimitText(offload?.engine_queue_limit));
    setDraftMaxInput(storedLimitText(offload?.max_input_tokens));
  }, [modelId, offload]);

  const maxWaitSeconds = queue?.maxWaitSeconds ?? null;
  const validatedWait = validateWait(draftWait);
  const validatedLimit = validateLimit(draftLimit);
  const validatedMaxInput = validateLimit(draftMaxInput);
  const storedRouteMissing =
    offload !== null && !routes.some((route) => route.route_id === offload.route_id);
  const hasOtherRoute = routes.length >= 2;
  const dirty =
    offload === null
      ? draftRouteId !== ''
      : draftRouteId !== offload.route_id ||
        (validatedWait.ok && validatedWait.value !== offload.wait_seconds) ||
        (validatedLimit.ok && validatedLimit.value !== (offload.engine_queue_limit ?? null)) ||
        (validatedMaxInput.ok && validatedMaxInput.value !== (offload.max_input_tokens ?? null));
  const canSave =
    editable &&
    hasOtherRoute &&
    draftRouteId !== '' &&
    validatedWait.ok &&
    validatedLimit.ok &&
    validatedMaxInput.ok &&
    dirty &&
    !saving;

  const status = offload === null ? null : offload.active ? 'Active' : 'Inactive';

  const onSave = async () => {
    if (!validatedWait.ok || !validatedLimit.ok || !validatedMaxInput.ok || !draftRouteId) return;
    setSaving(true);
    try {
      const updated = await setOffloadRoute(modelId, draftRouteId, validatedWait.value, {
        engineQueueLimit: validatedLimit.value,
        maxInputTokens: validatedMaxInput.value,
      });
      onChange(modelId, updated.offload);
      toast.success('Offload route saved');
    } catch (err) {
      toast.error(`Offload route update failed: ${getErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const onClear = async () => {
    setSaving(true);
    try {
      await clearOffloadRoute(modelId);
      onChange(modelId, null);
      toast.success('Offload route cleared');
    } catch (err) {
      toast.error(`Offload route clear failed: ${getErrorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section
      className="rounded-lg border border-gray-200 bg-white p-4"
      data-testid="offload-route-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-3xl">
          <h3 className="text-[14px] font-semibold text-gray-900">Queue offload</h3>
          <p className="mt-1 text-[12px] leading-5 text-gray-500">
            Reserve one route for requests the others cannot take. When a request has waited this
            long for an outbound slot on its route, or a streaming request has waited this long for
            its engine&apos;s first token, it is sent to the offload route instead. Each request is
            judged on its own wait, so the route that kept one waiting still gets the next. The
            offload route gets no ordinary traffic, and it is also the last resort when every other
            route fails.
          </p>
        </div>
        {status && (
          <span
            className={
              offload?.active
                ? 'inline-flex rounded bg-violet-50 px-1.5 py-0.5 text-[11px] font-medium text-violet-700'
                : 'inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700'
            }
            data-testid="offload-route-status"
          >
            {status}
          </span>
        )}
      </div>

      {loadError && (
        <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700" role="alert">
          Failed to load offload routes: {loadError}
        </div>
      )}

      {offload && !offload.active && offload.inactive_reason && (
        <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
          {offload.inactive_reason}
        </div>
      )}

      {queue && !queue.enabled && (
        <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
          The outbound limiter is disabled (UPSTREAM_CONCURRENCY_ENABLED=false), so no request waits
          in the gateway&apos;s queue for a remote route. A local engine under an engine queue limit
          still holds requests there, streaming requests are still offloaded when their engine sends
          no first token in time, and the offload route is the last resort.
        </div>
      )}

      {editable && !hasOtherRoute && (
        <div className="mt-3 rounded-lg border border-dashed border-gray-200 px-3 py-2 text-[12px] text-gray-500">
          Add another route first: the offload route gets no ordinary traffic, so the model needs at
          least one other route.
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-2">
        {editable && (
          <>
            <label className="flex min-w-[240px] flex-col gap-1 text-[12px] font-medium text-gray-500">
              Offload route
              <select
                aria-label="Offload route"
                value={draftRouteId}
                onChange={(event) => setDraftRouteId(event.target.value)}
                disabled={saving || !hasOtherRoute}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-normal text-gray-900 focus:border-gray-400 focus:outline-none disabled:opacity-50"
              >
                <option value="" disabled>
                  Choose a route…
                </option>
                {routes.map((route) => (
                  <option key={route.route_id} value={route.route_id}>
                    {routeLabel(route)}
                  </option>
                ))}
                {storedRouteMissing && offload && (
                  <option value={offload.route_id}>{offload.route_id} (no longer a route)</option>
                )}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[12px] font-medium text-gray-500">
              Wait before offloading (s)
              <input
                aria-label="Offload wait seconds"
                type="number"
                min={0}
                step={0.5}
                value={draftWait}
                onChange={(event) => setDraftWait(event.target.value)}
                disabled={saving || !hasOtherRoute}
                className="h-9 w-28 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
              />
            </label>
            <label className="flex flex-col gap-1 text-[12px] font-medium text-gray-500">
              Engine queue limit
              <input
                aria-label="Engine queue limit"
                type="number"
                min={1}
                step={1}
                placeholder="Off"
                value={draftLimit}
                onChange={(event) => setDraftLimit(event.target.value)}
                disabled={saving || !hasOtherRoute}
                className="h-9 w-28 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
              />
            </label>
            <label className="flex flex-col gap-1 text-[12px] font-medium text-gray-500">
              Max offload input (tokens)
              <input
                aria-label="Max offload input tokens"
                type="number"
                min={1}
                step={1}
                placeholder="No limit"
                value={draftMaxInput}
                onChange={(event) => setDraftMaxInput(event.target.value)}
                disabled={saving || !hasOtherRoute}
                className="h-9 w-36 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
              />
            </label>
            <button
              type="button"
              onClick={() => void onSave()}
              disabled={!canSave}
              className="h-9 rounded-md bg-gray-900 px-3 text-[12px] font-medium text-white disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save offload route'}
            </button>
          </>
        )}
        {offload && (
          <button
            type="button"
            onClick={() => void onClear()}
            disabled={saving}
            className="h-9 rounded-md border border-gray-300 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
          >
            Clear offload route
          </button>
        )}
      </div>

      {editable && !validatedWait.ok && draftWait !== '' && (
        <p className="mt-1 text-[11px] text-red-600" role="alert">
          {validatedWait.error}
        </p>
      )}

      {editable && !validatedLimit.ok && (
        <p className="mt-1 text-[11px] text-red-600" role="alert">
          Engine queue limit: {validatedLimit.error}
        </p>
      )}

      {editable && !validatedMaxInput.ok && (
        <p className="mt-1 text-[11px] text-red-600" role="alert">
          Max offload input: {validatedMaxInput.error}
        </p>
      )}

      {editable && (
        <p className="mt-2 text-[11px] leading-5 text-gray-400">
          {maxWaitSeconds !== null
            ? `The outbound queue for a remote route gives up at ${maxWaitSeconds}s, its acquire timeout, and offloads the request then; a longer wait gives a local engine longer to start answering. `
            : ''}
          The engine&apos;s wait starts when the request leaves the gateway&apos;s queue. A local
          inference server queues there only under an engine queue limit: the gateway then sends it
          at most that many streaming requests still waiting for a first token and holds the rest in
          line, where a request that can use the offload route leaves after the wait, before the
          engine sees it, and one that cannot waits its turn. Leave the limit empty to send every
          request straight to the engine. Only streaming requests are watched or held.
        </p>
      )}

      {editable && (
        <p className="mt-2 text-[11px] leading-5 text-gray-400">
          A request whose prompt is estimated at more than the max offload input is never sent to
          the offload route, not even as the last resort: it waits for the model&apos;s other routes
          however long they take, and fails if none of them can take it. The estimate counts
          everything the request sends, at about 4 bytes of text a token, so it can differ from the
          provider&apos;s own count. Leave it empty to offload requests of any size.
        </p>
      )}
    </section>
  );
}
