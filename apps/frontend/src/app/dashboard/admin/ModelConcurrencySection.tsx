'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import type { AdminModelConcurrencyItem } from '@/lib/api/admin';
import { listModelConcurrency, updateModelConcurrency } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const SECTION = 'Model Concurrency Limit';

interface ModelConcurrencySectionProps {
  onToast: (msg: string) => void;
}

const exemptLabel = (exempt: boolean) => (exempt ? 'Exempt' : 'Not exempt');

/**
 * Per-model exemption from the per-user concurrency limit. Read-only until
 * Edit; the changed models are saved together after confirmation.
 */
export function ModelConcurrencySection({ onToast }: ModelConcurrencySectionProps) {
  const queryClient = useQueryClient();
  const confirmation = useConfirmAction();
  const [models, setModels] = useState<AdminModelConcurrencyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, boolean>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listModelConcurrency();
      setModels(response.models);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const changeDraft = (model: AdminModelConcurrencyItem, exempt: boolean) => {
    setDrafts((prev) => {
      const next = { ...prev };
      if (exempt === model.exempt) delete next[model.model_id];
      else next[model.model_id] = exempt;
      return next;
    });
  };

  const cancel = () => {
    if (confirmation.busy) return;
    setDrafts({});
    setSaveError(null);
    setEditing(false);
  };

  const changed = models.filter((model) => model.model_id in drafts);

  const save = () => {
    if (changed.length === 0) return;
    const pending = changed.map((model) => ({ model, exempt: drafts[model.model_id] }));
    confirmation.request({
      title: `Save ${plural(pending.length, 'concurrency change')}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ model, exempt }) => ({
              label: model.model_id,
              before: exemptLabel(model.exempt),
              after: exemptLabel(exempt),
            }))}
          />
          <p>
            Requests to an exempt model stop counting toward a user&apos;s in-flight limit, up to 64
            concurrent requests per user.
          </p>
        </>
      ),
      run: async () => {
        const failure = await writeEach(
          pending,
          ({ model }) => model.model_id,
          async ({ model, exempt }) => {
            const updated = await updateModelConcurrency(model.model_id, exempt);
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
        void queryClient.invalidateQueries({ queryKey: ['user', 'models'] });
        setSaveError(failure);
        if (failure) return;
        setEditing(false);
        onToast(
          pending.length === 1
            ? `Updated concurrency exemption for ${pending[0].model.model_id}.`
            : `Updated concurrency exemption for ${pending.length} models.`,
        );
      },
    });
  };

  return (
    <section
      aria-label={SECTION}
      className={`rounded-xl border bg-white p-5 ${editing ? 'border-gray-400 shadow-sm' : 'border-gray-200'}`}
    >
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="text-[14px] font-semibold text-gray-900">{SECTION}</h2>
          <p className="mt-1 text-[12px] text-gray-500">
            Exempt a model from the per-user concurrency limit. Requests to an exempt model do not
            count toward a user&apos;s normal in-flight request cap, but are still limited to at
            most 64 concurrent requests per user.
          </p>
        </div>
        {!editing && !loading && models.length > 0 ? (
          <EditButton target={SECTION} onClick={() => setEditing(true)} />
        ) : null}
      </div>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600">
          {error}
          <button type="button" onClick={() => void load()} className="ml-2 font-medium underline">
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[13px] text-gray-400">Loading...</div>
      ) : models.length === 0 ? (
        <div className="rounded-md border border-dashed border-gray-200 px-4 py-6 text-center text-[12px] text-gray-500">
          No models available.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-gray-200">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50 text-gray-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Model</th>
                <th className="px-3 py-2 text-right font-medium">Exempt</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const exempt = drafts[model.model_id] ?? model.exempt;
                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-3 py-2 text-gray-900">{model.model_id}</td>
                    <td className="px-3 py-2 text-right">
                      {editing ? (
                        <input
                          type="checkbox"
                          aria-label={`Concurrency exemption for ${model.model_id}`}
                          className="h-4 w-4 rounded border-gray-300"
                          disabled={confirmation.busy}
                          checked={exempt}
                          onChange={(e) => changeDraft(model, e.target.checked)}
                        />
                      ) : (
                        <output aria-label={`Concurrency exemption for ${model.model_id}`}>
                          {exemptLabel(model.exempt)}
                        </output>
                      )}
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
          changeCount={changed.length}
          onCancel={cancel}
          onSave={save}
          busy={confirmation.busy}
          error={saveError}
        />
      ) : null}
      {confirmation.dialog}
    </section>
  );
}
