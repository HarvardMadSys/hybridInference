'use client';

import { useCallback, useEffect, useState } from 'react';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  CONTROL,
  FIELD_INVALID,
  NOTICE_ACTION,
  PANEL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import type { AdminBatchModelLimit } from '@/lib/api/admin';
import {
  listBatchModelLimits,
  MAX_BATCH_MODEL_CONCURRENCY,
  updateBatchModelLimit,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { validateNumericSettingInput } from './numericSettingValidation';

const SECTION = 'Batch Concurrency';

interface BatchConcurrencySectionProps {
  onToast: (msg: string) => void;
}

/** A cap as the table and the confirmation show it; `null` is the default. */
function capLabel(cap: number | null, defaultCap: number): string {
  if (cap === null) return `Default (${defaultCap === 0 ? '0, paused' : defaultCap})`;
  return cap === 0 ? '0 (paused)' : String(cap);
}

/** The saved cap as the input holds it: empty for the default. */
const savedText = (model: AdminBatchModelLimit) =>
  model.max_concurrency === null ? '' : String(model.max_concurrency);

type ParsedCap = { ok: true; value: number | null } | { ok: false; error: string };

/** Empty clears the cap (the default applies); otherwise a whole number in range. */
function parseCap(raw: string): ParsedCap {
  if (raw.trim() === '') return { ok: true, value: null };
  const validated = validateNumericSettingInput(raw, {
    min: 0,
    max: MAX_BATCH_MODEL_CONCURRENCY,
    integer: true,
  });
  return validated.ok ? { ok: true, value: validated.value } : validated;
}

/**
 * How many batch requests each model may run at once, across the deployment.
 * Read-only until Edit; the changed models are saved after the administrator
 * confirms the list, one request per model.
 */
export function BatchConcurrencySection({ onToast }: BatchConcurrencySectionProps) {
  const confirmation = useConfirmAction();
  const [models, setModels] = useState<AdminBatchModelLimit[]>([]);
  const [defaultCap, setDefaultCap] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Model id → the typed cap ('' for the default); only edits away from the saved value are kept.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listBatchModelLimits();
      setModels(response.models);
      setDefaultCap(response.default_concurrency);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const changeDraft = (model: AdminBatchModelLimit, text: string) => {
    setDrafts((prev) => {
      const next = { ...prev };
      if (text === savedText(model)) delete next[model.model_id];
      else next[model.model_id] = text;
      return next;
    });
  };

  const cancel = () => {
    if (confirmation.busy) return;
    setDrafts({});
    setSaveError(null);
    setEditing(false);
  };

  // A model is changed when its draft is valid and differs from what is saved.
  const changes = models.flatMap((model) => {
    if (!(model.model_id in drafts)) return [];
    const parsed = parseCap(drafts[model.model_id]);
    if (!parsed.ok || parsed.value === model.max_concurrency) return [];
    return [{ model, value: parsed.value }];
  });
  const invalid = models.filter(
    (model) => model.model_id in drafts && !parseCap(drafts[model.model_id]).ok,
  );

  const save = () => {
    if (invalid.length > 0) {
      setSaveError(
        `Correct the cap for ${invalid.map((model) => model.model_id).join(', ')} before saving.`,
      );
      return;
    }
    if (changes.length === 0) return;
    setSaveError(null);
    const pending = changes;
    confirmation.request({
      title: `Save ${plural(pending.length, 'batch concurrency change')}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ model, value }) => ({
              label: model.model_id,
              before: capLabel(model.max_concurrency, defaultCap),
              after: capLabel(value, defaultCap),
            }))}
          />
          <p>
            Every gateway process applies the new caps within seconds. Batch requests already
            running finish; a lower cap holds back the rest.
          </p>
        </>
      ),
      run: async () => {
        const failure = await writeEach(
          pending,
          ({ model }) => model.model_id,
          async ({ model, value }) => {
            const updated = await updateBatchModelLimit(model.model_id, value);
            setModels((prev) =>
              prev.map((item) => (item.model_id === model.model_id ? updated : item)),
            );
            setDrafts((prev) => {
              const next = { ...prev };
              delete next[model.model_id];
              return next;
            });
          },
        );
        setSaveError(failure);
        if (failure) return;
        setEditing(false);
        onToast(
          pending.length === 1
            ? `Updated batch concurrency for ${pending[0].model.model_id}.`
            : `Updated batch concurrency for ${pending.length} models.`,
        );
      },
    });
  };

  return (
    <AdminSection
      title={SECTION}
      description="How many batch requests each model may run at once. A cap is shared by every gateway process and applies within seconds; an empty cap uses the default and 0 pauses batch work on the model. The default, and who may submit batches, are set in Admin → Configuration → Batch inference."
      actions={
        !editing && !loading && models.length > 0 ? (
          <EditButton target={SECTION} onClick={() => setEditing(true)} />
        ) : null
      }
    >
      {error && (
        <div className="mb-3">
          <AdminNotice
            action={
              <button type="button" onClick={() => void load()} className={NOTICE_ACTION}>
                Retry
              </button>
            }
          >
            {error}
          </AdminNotice>
        </div>
      )}

      {loading ? (
        <AdminLoading size="sm" />
      ) : models.length === 0 ? (
        <AdminEmptyState title="No models available." />
      ) : (
        <div
          className={`${PANEL} relative overflow-x-auto ${editing ? 'border-gray-400 shadow-sm' : ''}`}
        >
          <table className="w-full min-w-[36rem] text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Model</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Max concurrent batch requests</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Running</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Queued</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const label = `Max concurrent batch requests for ${model.model_id}`;
                const draft = drafts[model.model_id] ?? savedText(model);
                const parsed = parseCap(draft);
                const changed = model.model_id in drafts;
                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-gray-900">
                      {model.model_id}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {editing ? (
                        <>
                          <input
                            type="number"
                            min={0}
                            max={MAX_BATCH_MODEL_CONCURRENCY}
                            step={1}
                            aria-label={label}
                            aria-invalid={!parsed.ok || undefined}
                            placeholder={capLabel(null, defaultCap)}
                            className={`${CONTROL} w-32 text-right ${
                              !parsed.ok ? FIELD_INVALID : changed ? 'border-amber-400' : ''
                            }`}
                            disabled={confirmation.busy}
                            value={draft}
                            onChange={(e) => changeDraft(model, e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                e.preventDefault();
                                save();
                              }
                            }}
                          />
                          {!parsed.ok && (
                            <p className="mt-0.5 text-[11px] text-red-600" role="alert">
                              {parsed.error}
                            </p>
                          )}
                        </>
                      ) : (
                        <output
                          aria-label={label}
                          className={
                            model.max_concurrency === null
                              ? 'text-gray-500'
                              : 'tabular-nums text-gray-900'
                          }
                        >
                          {capLabel(model.max_concurrency, defaultCap)}
                        </output>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-gray-700">
                      {model.running}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-gray-700">
                      {model.queued}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {editing ? (
        <EditBar
          target={SECTION}
          changeCount={changes.length}
          onCancel={cancel}
          onSave={save}
          busy={confirmation.busy}
          error={saveError}
        />
      ) : null}
      {confirmation.dialog}
    </AdminSection>
  );
}
