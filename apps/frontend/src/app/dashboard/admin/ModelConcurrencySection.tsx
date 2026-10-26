'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  CHECKBOX,
  NOTICE_ACTION,
  PANEL,
  TABLE_HEAD_CELL,
} from '@/components/features/admin/adminStyles';
import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
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
 * Per-model exemption from the per-user concurrency limit. The checkboxes are
 * edited in place; the changed models are saved together after confirmation.
 */
export function ModelConcurrencySection({ onToast }: ModelConcurrencySectionProps) {
  const queryClient = useQueryClient();
  const confirmation = useConfirmAction();
  const [models, setModels] = useState<AdminModelConcurrencyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
        onToast(
          pending.length === 1
            ? `Updated concurrency exemption for ${pending[0].model.model_id}.`
            : `Updated concurrency exemption for ${pending.length} models.`,
        );
      },
    });
  };

  return (
    <AdminSection
      title={SECTION}
      description="Exempt a model from the per-user concurrency limit. Requests to an exempt model do not count toward a user's normal in-flight request cap, but are still limited to at most 64 concurrent requests per user."
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
          className={`${PANEL} relative overflow-x-auto ${changed.length > 0 ? 'border-gray-400 shadow-sm' : ''}`}
        >
          <table className="w-full min-w-[28rem] text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Model</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Exempt</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const exempt = drafts[model.model_id] ?? model.exempt;
                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-gray-900">
                      {model.model_id}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <input
                        type="checkbox"
                        aria-label={`Concurrency exemption for ${model.model_id}`}
                        className={CHECKBOX}
                        disabled={confirmation.busy}
                        checked={exempt}
                        onChange={(e) => changeDraft(model, e.target.checked)}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <EditBar
        target={SECTION}
        changeCount={changed.length}
        onCancel={cancel}
        onSave={save}
        busy={confirmation.busy}
        error={saveError}
        inPlace
      />
      {confirmation.dialog}
    </AdminSection>
  );
}
