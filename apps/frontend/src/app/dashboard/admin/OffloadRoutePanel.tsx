'use client';

import { useEffect, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { useReportEditing } from '@/components/features/admin/confirm/useReportEditing';
import type { OffloadRoute, ProviderRoute } from '@/lib/api/admin';
import { clearOffloadRoute, setOffloadRoute } from '@/lib/api/admin';
import { validateNumericSettingInput } from './numericSettingValidation';
import { Select } from '@/components/ui/Select';

const SECTION = 'Queue offload';

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
  /**
   * Whether a change is unsaved, a confirmation open or a write running, so the
   * parent can hold the model selection.
   */
  onEditingChange?: (active: boolean) => void;
}

/** The fields as typed; a field left out shows what is stored. */
type OffloadDrafts = Partial<Record<'routeId' | 'wait' | 'limit' | 'maxInput', string>>;

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
  onEditingChange,
}: OffloadRoutePanelProps) {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [saveError, setSaveError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<OffloadDrafts>({});

  // The form shows what is stored, or the defaults for a new route, until a
  // field is changed.
  const stored: Required<OffloadDrafts> = {
    routeId: offload?.route_id ?? '',
    wait: offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS,
    limit: storedLimitText(offload?.engine_queue_limit),
    maxInput: storedLimitText(offload?.max_input_tokens),
  };
  const draftRouteId = drafts.routeId ?? stored.routeId;
  const draftWait = drafts.wait ?? stored.wait;
  const draftLimit = drafts.limit ?? stored.limit;
  const draftMaxInput = drafts.maxInput ?? stored.maxInput;

  // A field set back to what it shows unchanged is no draft.
  const changeDraft = (field: keyof OffloadDrafts, value: string) =>
    setDrafts((current) => {
      const next = { ...current };
      if (value === stored[field]) delete next[field];
      else next[field] = value;
      return next;
    });

  // Another model's drafts are never carried over.
  useEffect(() => {
    setDrafts({});
    setSaveError(null);
  }, [modelId]);

  const maxWaitSeconds = queue?.maxWaitSeconds ?? null;
  const validatedWait = validateWait(draftWait);
  const validatedLimit = validateLimit(draftLimit);
  const validatedMaxInput = validateLimit(draftMaxInput);
  const storedRouteMissing =
    offload !== null && !routes.some((route) => route.route_id === offload.route_id);
  const hasOtherRoute = routes.length >= 2;
  const busy = confirmation.busy;

  const routeName = (routeId: string) => {
    const route = routes.find((candidate) => candidate.route_id === routeId);
    return route ? routeLabel(route) : `${routeId} (no longer a route)`;
  };

  // The fields of a stored route that differ from what is stored. A field
  // typed back to its stored value is no change. An invalid field counts as
  // one, so Save can say what to fix instead of doing nothing.
  const changedFields =
    offload === null || !editable || draftRouteId === ''
      ? 0
      : [
          draftRouteId !== offload.route_id,
          !validatedWait.ok || validatedWait.value !== offload.wait_seconds,
          !validatedLimit.ok || validatedLimit.value !== (offload.engine_queue_limit ?? null),
          !validatedMaxInput.ok || validatedMaxInput.value !== (offload.max_input_tokens ?? null),
        ].filter(Boolean).length;
  // A new offload route is one change, whatever its prefilled settings. Its
  // other fields can be filled in first, but it can be saved only with a route.
  const newRouteTouched =
    offload === null &&
    editable &&
    (draftRouteId !== '' ||
      draftWait !== stored.wait ||
      draftLimit !== stored.limit ||
      draftMaxInput !== stored.maxInput);
  const changeCount = offload === null ? (newRouteTouched ? 1 : 0) : changedFields;
  const changed = changeCount > 0;
  const routeMissing = newRouteTouched && draftRouteId === '';
  useReportEditing(changed || confirmation.open || confirmation.busy, onEditingChange);

  const status = offload === null ? null : offload.active ? 'Active' : 'Inactive';

  const discard = () => {
    if (busy) return;
    setDrafts({});
    setSaveError(null);
  };

  // Save writes at once. A failure shows on the bar, and the drafts stay.
  const onSave = async () => {
    if (changeCount === 0) return;
    if (!validatedWait.ok || !validatedLimit.ok || !validatedMaxInput.ok) {
      setSaveError('Fix the values marked below before saving.');
      return;
    }
    if (!editable || !hasOtherRoute || !draftRouteId) return;
    setSaveError(null);
    const routeId = draftRouteId;
    const wait = validatedWait.value;
    const limits = {
      engineQueueLimit: validatedLimit.value,
      maxInputTokens: validatedMaxInput.value,
    };
    const failure = await confirmation.perform({
      run: async () => {
        const updated = await setOffloadRoute(modelId, routeId, wait, limits);
        onChange(modelId, updated.offload);
        setDrafts({});
        toast.success('Offload route saved');
      },
    });
    setSaveError(failure);
  };

  const onClear = () => {
    if (!offload) return;
    confirmation.request({
      title: `Clear the offload route for ${modelId}?`,
      body: (
        <p>
          {routeName(offload.route_id)} is no longer reserved: requests that wait too long are no
          longer sent to it, and it is no longer the last resort when every other route fails.
        </p>
      ),
      confirmLabel: 'Clear',
      busyLabel: 'Clearing…',
      tone: 'danger',
      run: async () => {
        await clearOffloadRoute(modelId);
        onChange(modelId, null);
        setDrafts({});
        toast.success('Offload route cleared');
      },
    });
  };

  return (
    <section
      aria-label={SECTION}
      className={`rounded-lg border bg-white p-4 ${changed ? 'border-gray-400 shadow-sm' : 'border-gray-200'}`}
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
        <div className="flex items-center gap-2">
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

      {editable && (
        // Without a second route there is nothing to reserve: the fields stay
        // disabled and show what is stored.
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <label className="flex min-w-[240px] flex-col gap-1 text-[12px] font-medium text-gray-500">
            Offload route
            {/* A choice only changes the draft; nothing is written before Save. */}
            <Select
              aria-label="Offload route"
              value={draftRouteId}
              onValueChange={(value) => changeDraft('routeId', value)}
              disabled={busy || !hasOtherRoute}
              placeholder="Choose a route…"
              options={[
                ...routes.map((route) => ({ value: route.route_id, label: routeLabel(route) })),
                ...(storedRouteMissing && offload
                  ? [
                      {
                        value: offload.route_id,
                        label: `${offload.route_id} (no longer a route)`,
                      },
                    ]
                  : []),
              ]}
              size="lg"
              className="w-full font-normal"
            />
          </label>
          <label className="flex flex-col gap-1 text-[12px] font-medium text-gray-500">
            Wait before offloading (s)
            <input
              aria-label="Offload wait seconds"
              type="number"
              min={0}
              step={0.5}
              value={draftWait}
              onChange={(event) => changeDraft('wait', event.target.value)}
              disabled={busy || !hasOtherRoute}
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
              onChange={(event) => changeDraft('limit', event.target.value)}
              disabled={busy || !hasOtherRoute}
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
              onChange={(event) => changeDraft('maxInput', event.target.value)}
              disabled={busy || !hasOtherRoute}
              className="h-9 w-36 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
            />
          </label>
        </div>
      )}

      {offload && (
        <div className="mt-3">
          <button
            type="button"
            onClick={onClear}
            // Clearing replaces what the drafts were typed against.
            disabled={busy || changed}
            title={changed ? 'Save or discard the unsaved changes first' : undefined}
            className="h-9 rounded-md border border-gray-300 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
          >
            Clear offload route
          </button>
        </div>
      )}

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
      {editable ? (
        <EditBar
          target={SECTION}
          changeCount={changeCount}
          onCancel={discard}
          onSave={() => void onSave()}
          busy={busy}
          error={saveError}
          blocked={routeMissing ? 'Choose the offload route to save these settings' : null}
          inPlace
        />
      ) : null}
      {confirmation.dialog}
    </section>
  );
}
