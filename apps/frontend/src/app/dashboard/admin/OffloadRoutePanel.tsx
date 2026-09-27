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

function validateWait(raw: string, maxWaitSeconds: number | null) {
  const validated = validateNumericSettingInput(raw, {
    min: null,
    max: maxWaitSeconds,
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
  const [saving, setSaving] = useState(false);

  // Start over whenever the model, or what is stored for it, changes.
  useEffect(() => {
    setDraftRouteId(offload?.route_id ?? '');
    setDraftWait(offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS);
  }, [modelId, offload]);

  const maxWaitSeconds = queue?.maxWaitSeconds ?? null;
  const validatedWait = validateWait(draftWait, maxWaitSeconds);
  const storedRouteMissing =
    offload !== null && !routes.some((route) => route.route_id === offload.route_id);
  const hasOtherRoute = routes.length >= 2;
  const dirty =
    offload === null
      ? draftRouteId !== ''
      : draftRouteId !== offload.route_id ||
        (validatedWait.ok && validatedWait.value !== offload.wait_seconds);
  const canSave =
    editable && hasOtherRoute && draftRouteId !== '' && validatedWait.ok && dirty && !saving;

  const status = offload === null ? null : offload.active ? 'Active' : 'Inactive';
  const stalledLabels = (offload?.stalled_endpoints ?? []).map((endpointId) => {
    const route = routes.find((candidate) => candidate.endpoint_id === endpointId);
    return route ? routeLabel(route) : endpointId;
  });

  const onSave = async () => {
    if (!validatedWait.ok || !draftRouteId) return;
    setSaving(true);
    try {
      const updated = await setOffloadRoute(modelId, draftRouteId, validatedWait.value);
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
            its engine&apos;s first token, it is sent to the offload route instead, and that engine
            gets no new requests until it answers again. The offload route gets no ordinary traffic,
            and it is also the last resort when every other route fails.
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

      {offload?.active && stalledLabels.length > 0 && (
        <div
          className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800"
          data-testid="offload-stalled-routes"
        >
          Stalled now: {stalledLabels.join(', ')}. New requests go around{' '}
          {stalledLabels.length === 1 ? 'it' : 'them'} until a request already there gets its first
          token.
        </div>
      )}

      {queue && !queue.enabled && (
        <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
          The outbound limiter is disabled (UPSTREAM_CONCURRENCY_ENABLED=false), so no request waits
          in the gateway&apos;s queue. Streaming requests are still offloaded when their engine
          sends no first token in time, and the offload route is the last resort.
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
                max={maxWaitSeconds ?? undefined}
                step={0.5}
                value={draftWait}
                onChange={(event) => setDraftWait(event.target.value)}
                disabled={saving || !hasOtherRoute}
                className="h-9 w-28 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
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

      {editable && (
        <p className="mt-2 text-[11px] leading-5 text-gray-400">
          {maxWaitSeconds !== null
            ? `At most ${maxWaitSeconds}s, the outbound queue's acquire timeout: a request that waits that long is offloaded anyway. `
            : ''}
          The engine&apos;s wait starts when the request leaves the gateway&apos;s queue, so a local
          inference server, which never queues there, gets the whole wait. Only streaming requests
          are watched for a first token.
        </p>
      )}
    </section>
  );
}
