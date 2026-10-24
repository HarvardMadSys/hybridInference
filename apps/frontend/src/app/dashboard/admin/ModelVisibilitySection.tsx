'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import type { AdminModelVisibilityItem, Role } from '@/lib/api/admin';
import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const DEFAULT_VALUE = '__default__';
const SECTION = 'Model Visibility';

const ROLE_OPTIONS: Array<{ value: Role; label: Role }> = [
  { value: 'free', label: 'free' },
  { value: 'pro', label: 'pro' },
  { value: 'internal', label: 'internal' },
  { value: 'admin', label: 'admin' },
];

interface ModelVisibilitySectionProps {
  onToast: (msg: string) => void;
}

function overrideLabel(value: string | null): string {
  return value === null || value === DEFAULT_VALUE ? 'Use default' : value;
}

/**
 * Runtime required-role overrides. Read-only until Edit; the changed models are
 * saved together after the administrator confirms the list.
 */
export function ModelVisibilitySection({ onToast }: ModelVisibilitySectionProps) {
  const queryClient = useQueryClient();
  const confirmation = useConfirmAction();
  const [models, setModels] = useState<AdminModelVisibilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Model id → the chosen override (DEFAULT_VALUE for none); only real changes are kept.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listModelVisibility();
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

  const savedValue = (model: AdminModelVisibilityItem) =>
    model.override_required_role ?? DEFAULT_VALUE;

  const changeDraft = (model: AdminModelVisibilityItem, value: string) => {
    setDrafts((prev) => {
      const next = { ...prev };
      if (value === savedValue(model)) delete next[model.model_id];
      else next[model.model_id] = value;
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
    const pending = changed.map((model) => ({ model, value: drafts[model.model_id] }));
    confirmation.request({
      title: `Save ${plural(pending.length, 'visibility change')}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ model, value }) => ({
              label: model.model_id,
              before: overrideLabel(model.override_required_role),
              after: overrideLabel(value),
            }))}
          />
          <p>Users below the required role stop seeing a model as soon as this is saved.</p>
        </>
      ),
      run: async () => {
        const failure = await writeEach(
          pending,
          ({ model }) => model.model_id,
          async ({ model, value }) => {
            const updated = await updateModelVisibility(
              model.model_id,
              value === DEFAULT_VALUE ? null : (value as Role),
            );
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
            ? `Updated visibility for ${pending[0].model.model_id}.`
            : `Updated visibility for ${pending.length} models.`,
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
            Override each model&apos;s runtime required role without changing the YAML baseline.
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
                <th className="px-3 py-2 text-left font-medium">Baseline</th>
                <th className="px-3 py-2 text-left font-medium">Override</th>
                <th className="px-3 py-2 text-left font-medium">Effective</th>
                <th className="px-3 py-2 text-right font-medium">Runtime Override</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const selected = drafts[model.model_id] ?? savedValue(model);
                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-3 py-2 text-gray-900">{model.model_id}</td>
                    <td className="px-3 py-2 text-gray-500">{model.baseline_required_role}</td>
                    <td className="px-3 py-2 text-gray-500">
                      {model.override_required_role ?? 'Use default'}
                    </td>
                    <td className="px-3 py-2 text-gray-900">{model.effective_required_role}</td>
                    <td className="px-3 py-2 text-right">
                      {editing ? (
                        <select
                          aria-label={`Runtime override for ${model.model_id}`}
                          className={`rounded-md border px-2 py-1 text-[13px] text-gray-900 ${
                            model.model_id in drafts ? 'border-amber-400' : 'border-gray-300'
                          }`}
                          disabled={confirmation.busy}
                          value={selected}
                          onChange={(e) => changeDraft(model, e.target.value)}
                        >
                          <option value={DEFAULT_VALUE}>Use default</option>
                          {ROLE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <output aria-label={`Runtime override for ${model.model_id}`}>
                          {overrideLabel(model.override_required_role)}
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
