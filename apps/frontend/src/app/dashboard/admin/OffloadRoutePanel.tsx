'use client';

import { useEffect, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import { ChangeList, plural, type Change } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
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
  /** Whether an edit or confirmation is open, so the parent can hold the model selection. */
  onEditingChange?: (active: boolean) => void;
}

type ValidatedLimit = { ok: true; value: number | null } | { ok: false; error: string };

function storedLimitText(limit: number | null | undefined): string {
  return limit === null || limit === undefined ? '' : String(limit);
}

function limitLabel(limit: number | null | undefined, none: string): string {
  return limit === null || limit === undefined ? none : String(limit);
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
  const [editing, setEditing] = useState(false);
  useReportEditing(editing || confirmation.open, onEditingChange);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [draftRouteId, setDraftRouteId] = useState(offload?.route_id ?? '');
  const [draftWait, setDraftWait] = useState(
    offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS,
  );
  const [draftLimit, setDraftLimit] = useState(storedLimitText(offload?.engine_queue_limit));
  const [draftMaxInput, setDraftMaxInput] = useState(storedLimitText(offload?.max_input_tokens));

  // Drafts start from what is stored each time Edit opens; another model's
  // drafts are never carried over.
  const resetDrafts = () => {
    setDraftRouteId(offload?.route_id ?? '');
    setDraftWait(offload ? String(offload.wait_seconds) : DEFAULT_WAIT_SECONDS);
    setDraftLimit(storedLimitText(offload?.engine_queue_limit));
    setDraftMaxInput(storedLimitText(offload?.max_input_tokens));
  };

  useEffect(() => {
    setEditing(false);
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

  // The fields that differ from what is stored, `old → new`. A field typed
  // back to its stored value is no change. An invalid field counts as one, so
  // Save can say what to fix instead of doing nothing.
  const changes: Change[] = [];
  if (editing && draftRouteId !== '') {
    if (draftRouteId !== (offload?.route_id ?? '')) {
      changes.push({
        label: 'Offload route',
        before: offload ? routeName(offload.route_id) : 'None',
        after: routeName(draftRouteId),
      });
    }
    const waitBefore = offload ? offload.wait_seconds : null;
    if (!validatedWait.ok || validatedWait.value !== waitBefore) {
      changes.push({
        label: 'Wait before offloading',
        before: waitBefore === null ? 'None' : `${waitBefore}s`,
        after: validatedWait.ok ? `${validatedWait.value}s` : draftWait || '(empty)',
      });
    }
    const limitBefore = offload?.engine_queue_limit ?? null;
    if (!validatedLimit.ok || validatedLimit.value !== limitBefore) {
      changes.push({
        label: 'Engine queue limit',
        before: limitLabel(limitBefore, 'Off'),
        after: validatedLimit.ok ? limitLabel(validatedLimit.value, 'Off') : draftLimit,
      });
    }
    const maxInputBefore = offload?.max_input_tokens ?? null;
    if (!validatedMaxInput.ok || validatedMaxInput.value !== maxInputBefore) {
      changes.push({
        label: 'Max offload input',
        before: limitLabel(maxInputBefore, 'No limit'),
        after: validatedMaxInput.ok
          ? limitLabel(validatedMaxInput.value, 'No limit')
          : draftMaxInput,
      });
    }
  }
  // A new offload route is one change, whatever its prefilled settings.
  const changeCount = offload === null ? (changes.length > 0 ? 1 : 0) : changes.length;

  const status = offload === null ? null : offload.active ? 'Active' : 'Inactive';

  const startEdit = () => {
    resetDrafts();
    setSaveError(null);
    setEditing(true);
  };

  const cancelEdit = () => {
    if (busy) return;
    resetDrafts();
    setSaveError(null);
    setEditing(false);
  };

  const onSave = () => {
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
    const listed = changes;
    confirmation.request({
      title:
        offload === null
          ? `Set up the offload route for ${modelId}?`
          : `Save ${plural(listed.length, 'change')} to the ${modelId} offload route?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={listed} />
          <p>
            The offload route gets no ordinary traffic; requests that wait longer than this are sent
            to it as soon as it is saved.
          </p>
        </>
      ),
      run: async () => {
        const updated = await setOffloadRoute(modelId, routeId, wait, limits);
        onChange(modelId, updated.offload);
        setEditing(false);
        toast.success('Offload route saved');
      },
    });
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
        setEditing(false);
        toast.success('Offload route cleared');
      },
    });
  };

  return (
    <section
      aria-label={SECTION}
      className={`rounded-lg border bg-white p-4 ${editing ? 'border-gray-400 shadow-sm' : 'border-gray-200'}`}
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
          {editable && !editing && (
            <EditButton target={SECTION} onClick={startEdit} disabled={busy || !hasOtherRoute} />
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

      {editable && !editing && (
        <dl className="mt-3 grid gap-x-6 gap-y-2 text-[12px] sm:grid-cols-2 lg:grid-cols-4">
          <div className="min-w-0">
            <dt className="font-medium text-gray-500">Offload route</dt>
            <dd className="mt-0.5 text-[13px] text-gray-900 [overflow-wrap:anywhere]">
              <output aria-label="Offload route">
                {offload ? routeName(offload.route_id) : 'None'}
              </output>
            </dd>
          </div>
          <div>
            <dt className="font-medium text-gray-500">Wait before offloading</dt>
            <dd className="mt-0.5 text-[13px] text-gray-900">
              <output aria-label="Offload wait seconds">
                {offload ? `${offload.wait_seconds}s` : '—'}
              </output>
            </dd>
          </div>
          <div>
            <dt className="font-medium text-gray-500">Engine queue limit</dt>
            <dd className="mt-0.5 text-[13px] text-gray-900">
              <output aria-label="Engine queue limit">
                {offload ? limitLabel(offload.engine_queue_limit, 'Off') : '—'}
              </output>
            </dd>
          </div>
          <div>
            <dt className="font-medium text-gray-500">Max offload input (tokens)</dt>
            <dd className="mt-0.5 text-[13px] text-gray-900">
              <output aria-label="Max offload input tokens">
                {offload ? limitLabel(offload.max_input_tokens, 'No limit') : '—'}
              </output>
            </dd>
          </div>
        </dl>
      )}

      {editable && editing && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <label className="flex min-w-[240px] flex-col gap-1 text-[12px] font-medium text-gray-500">
            Offload route
            {/* A choice only changes the draft; nothing is written before Confirm. */}
            <Select
              aria-label="Offload route"
              value={draftRouteId}
              onValueChange={setDraftRouteId}
              disabled={busy}
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
              onChange={(event) => setDraftWait(event.target.value)}
              disabled={busy}
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
              disabled={busy}
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
              disabled={busy}
              className="h-9 w-36 rounded-md border border-gray-300 px-2 text-right text-[13px] font-normal text-gray-900 disabled:opacity-50"
            />
          </label>
        </div>
      )}

      {offload && !editing && (
        <div className="mt-3">
          <button
            type="button"
            onClick={onClear}
            disabled={busy}
            className="h-9 rounded-md border border-gray-300 px-3 text-[12px] font-medium text-gray-700 disabled:opacity-50"
          >
            Clear offload route
          </button>
        </div>
      )}

      {editable && editing && !validatedWait.ok && draftWait !== '' && (
        <p className="mt-1 text-[11px] text-red-600" role="alert">
          {validatedWait.error}
        </p>
      )}

      {editable && editing && !validatedLimit.ok && (
        <p className="mt-1 text-[11px] text-red-600" role="alert">
          Engine queue limit: {validatedLimit.error}
        </p>
      )}

      {editable && editing && !validatedMaxInput.ok && (
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
      {editing ? (
        <EditBar
          target={SECTION}
          changeCount={changeCount}
          onCancel={cancelEdit}
          onSave={onSave}
          busy={busy}
          error={saveError}
        />
      ) : null}
      {confirmation.dialog}
    </section>
  );
}
