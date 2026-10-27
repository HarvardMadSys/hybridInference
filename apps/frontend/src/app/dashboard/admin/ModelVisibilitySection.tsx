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
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import { Select, type SelectOption } from '@/components/ui/Select';
import type { AdminModelVisibilityItem, Role } from '@/lib/api/admin';
import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { useReportUnsaved } from './useReportUnsaved';

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
  /** Called with the number of unsaved changes whenever it changes. */
  onUnsavedChange?: (count: number) => void;
}

/**
 * Runtime required-role overrides, edited in place; Save writes the changed
 * models together.
 */
export function ModelVisibilitySection({ onToast, onUnsavedChange }: ModelVisibilitySectionProps) {
  const queryClient = useQueryClient();
  const writes = useConfirmAction();
  const [models, setModels] = useState<AdminModelVisibilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
    if (writes.busy) return;
    setDrafts({});
    setSaveError(null);
  };

  const changed = models.filter((model) => model.model_id in drafts);
  useReportUnsaved(changed.length, onUnsavedChange);

  const save = async () => {
    if (changed.length === 0) return;
    const pending = changed.map((model) => ({ model, value: drafts[model.model_id] }));
    setSaveError(null);
    const failure = await writes.perform({
      run: async () => {
        try {
          await writeEach(
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
        } finally {
          void queryClient.invalidateQueries({ queryKey: ['user', 'models'] });
        }
        onToast(
          pending.length === 1
            ? `Updated visibility for ${pending[0].model.model_id}.`
            : `Updated visibility for ${pending.length} models.`,
        );
      },
    });
    setSaveError(failure);
  };

  return (
    <AdminSection
      title={SECTION}
      description="Override each model's runtime required role without changing the YAML baseline."
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
                      {/* A choice only changes the draft; nothing is written before Save. */}
                      <Select
                        aria-label={`Runtime override for ${model.model_id}`}
                        className={`w-32 ${model.model_id in drafts ? 'border-amber-400' : ''}`}
                        disabled={writes.busy}
                        value={selected}
                        options={ROLE_OPTIONS}
                        onValueChange={(value) => changeDraft(model, value)}
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
        onSave={() => void save()}
        busy={writes.busy}
        error={saveError}
        inPlace
      />
    </AdminSection>
  );
}
