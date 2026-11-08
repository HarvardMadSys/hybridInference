'use client';

import { useId, useMemo } from 'react';
import { useModels } from '@/lib/hooks';
import { Button } from '@/components/ui/Button';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

type ModelChoice = { id: string; name: string; unavailable: boolean };

export function ModelPicker({
  value,
  onChange,
  title,
  hint,
  emptyMessage,
  disabled = false,
}: {
  value: string[] | null;
  onChange: (value: string[] | null) => void;
  title: string;
  hint?: string;
  emptyMessage?: string;
  disabled?: boolean;
}): JSX.Element {
  const t = useT();
  const group = useId();
  const { data: models, isLoading } = useModels();
  const restricted = value !== null;
  const selected = useMemo(() => new Set(value ?? []), [value]);

  // A model the catalog no longer lists stays visible, so saving does not drop
  // it without the owner seeing it.
  const choices = useMemo<ModelChoice[]>(() => {
    const listed = (models?.data ?? []).map((model) => ({
      id: model.id,
      name: model.name,
      unavailable: false,
    }));
    const known = new Set(listed.map((model) => model.id));
    const missing = (value ?? [])
      .filter((id) => !known.has(id))
      .map((id) => ({ id, name: id, unavailable: true }));
    return [...listed, ...missing];
  }, [models, value]);

  function toggle(id: string): void {
    const next = new Set(value ?? []);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange([...next].sort());
  }

  const noModels = restricted && selected.size === 0;

  return (
    <fieldset disabled={disabled}>
      <legend className="text-sm font-medium text-gray-700">{title}</legend>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:gap-6">
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="radio" name={group} checked={!restricted} onChange={() => onChange(null)} />
          {t('dashboard.model_picker.all_models', 'All models')}
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="radio" name={group} checked={restricted} onChange={() => onChange([])} />
          {t('dashboard.model_picker.some_models', 'Only the models selected below')}
        </label>
      </div>
      {restricted && (
        <>
          <div className="mt-3 flex items-center justify-between gap-3">
            <span className="text-xs text-gray-500">
              {fill(t('dashboard.model_picker.selected_count', '{count} selected'), {
                count: String(selected.size),
              })}
            </span>
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="secondary" onClick={() => onChange(null)}>
                {t('dashboard.model_picker.select_all', 'Select all')}
              </Button>
              <Button type="button" size="sm" variant="secondary" onClick={() => onChange([])}>
                {t('dashboard.model_picker.clear_all', 'Clear all')}
              </Button>
            </div>
          </div>
          <div className="mt-2 max-h-72 overflow-y-auto rounded-lg ring-1 ring-gray-200">
            {isLoading ? (
              <p className="p-3 text-sm text-gray-500">{t('common.loading', 'Loading...')}</p>
            ) : (
              <ul className="divide-y divide-gray-100">
                {choices.map((model) => (
                  <li key={model.id}>
                    <label className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-gray-50">
                      <input
                        type="checkbox"
                        checked={selected.has(model.id)}
                        onChange={() => toggle(model.id)}
                      />
                      <span className="min-w-0 flex-1 truncate text-gray-900">{model.name}</span>
                      <code className="truncate text-xs text-gray-500">{model.id}</code>
                      {model.unavailable && (
                        <span className="text-xs text-amber-700">
                          {t('dashboard.model_picker.unavailable', 'no longer available')}
                        </span>
                      )}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
      {noModels && (
        <p className="mt-2 text-xs text-red-600">
          {emptyMessage ??
            t(
              'dashboard.model_picker.pick_a_model',
              'Pick at least one model, or choose All models.',
            )}
        </p>
      )}
      {restricted && hint && <p className="mt-2 text-xs text-gray-500">{hint}</p>}
    </fieldset>
  );
}
