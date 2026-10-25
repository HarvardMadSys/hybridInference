'use client';

import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import { NOTICE_ACTION, PANEL, TABLE_HEAD_CELL } from '@/components/features/admin/adminStyles';
import { ChangeList, plural } from '@/components/features/admin/confirm/ChangeList';
import { EditBar, EditButton } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import { Select, type SelectOption } from '@/components/ui/Select';
import type { AdminModelVisibilityItem, Role } from '@/lib/api/admin';
import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

const DEFAULT_VALUE = '__default__';
const SECTION = 'Model Visibility';

const ROLE_OPTIONS: SelectOption[] = [
  { value: DEFAULT_VALUE, label: 'Use default' },
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
    <AdminSection
      title={SECTION}
      description="Override each model's runtime required role without changing the YAML baseline."
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
          <table className="w-full min-w-[28rem] text-[13px]">
            <thead className="bg-gray-50/80">
              <tr className="border-b border-gray-200">
                <th className={TABLE_HEAD_CELL}>Model</th>
                <th className={TABLE_HEAD_CELL}>Baseline</th>
                <th className={TABLE_HEAD_CELL}>Override</th>
                <th className={TABLE_HEAD_CELL}>Effective</th>
                <th className={`${TABLE_HEAD_CELL} text-right`}>Runtime Override</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((model) => {
                const selected = drafts[model.model_id] ?? savedValue(model);
                return (
                  <tr key={model.model_id} className="bg-white">
                    <td className="px-4 py-2 font-mono text-[12px] text-gray-900">
                      {model.model_id}
                    </td>
                    <td className="px-4 py-2 text-gray-500">{model.baseline_required_role}</td>
                    <td className="px-4 py-2 text-gray-500">
                      {model.override_required_role ?? 'Use default'}
                    </td>
                    <td className="px-4 py-2 text-gray-900">{model.effective_required_role}</td>
                    <td className="px-4 py-2 text-right">
                      {editing ? (
                        // A choice only changes the draft; nothing is written before Confirm.
                        <Select
                          aria-label={`Runtime override for ${model.model_id}`}
                          className={`w-32 ${model.model_id in drafts ? 'border-amber-400' : ''}`}
                          disabled={confirmation.busy}
                          value={selected}
                          options={ROLE_OPTIONS}
                          onValueChange={(value) => changeDraft(model, value)}
                        />
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
    </AdminSection>
  );
}
