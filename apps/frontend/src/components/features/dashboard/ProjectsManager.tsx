'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { useT } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  useCreateProject,
  useDeleteProject,
  useModels,
  useProjects,
  useUpdateProject,
} from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import type { Project, ProjectInput } from '@/lib/api/user';

function usd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `$${Number(value).toFixed(2)}`;
}

function BudgetBar({
  label,
  spent,
  limit,
}: {
  label: string;
  spent: string;
  limit: string | null;
}): JSX.Element {
  const spentValue = Number(spent);
  const limitValue = limit === null ? null : Number(limit);
  const pct =
    limitValue && limitValue > 0 ? Math.min(100, Math.round((spentValue / limitValue) * 100)) : 0;
  return (
    <div>
      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>{label}</span>
        <span>
          {usd(spent)} / {limit === null ? 'no project cap' : usd(limit)}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
        <div
          className={`h-full rounded-full ${pct >= 100 ? 'bg-red-500' : 'bg-gray-900'}`}
          style={{ width: `${limitValue ? pct : 0}%` }}
        />
      </div>
    </div>
  );
}

interface FormState {
  name: string;
  daily: string;
  monthly: string;
  allowedModels: string[];
}

const EMPTY_FORM: FormState = { name: '', daily: '', monthly: '', allowedModels: [] };

function ProjectForm({
  projectId,
  initial,
  modelIds,
  onClose,
}: {
  projectId: string | null;
  initial: FormState;
  modelIds: string[];
  onClose: () => void;
}): JSX.Element {
  const t = useT();
  const toast = useAccountToast();
  const [form, setForm] = useState<FormState>(initial);
  const createMutation = useCreateProject();
  const updateMutation = useUpdateProject();
  const isSaving = createMutation.isPending || updateMutation.isPending;

  function setAllowed(next: string[]): void {
    setForm((current) => ({ ...current, allowedModels: next }));
  }

  function toggleModel(id: string): void {
    setAllowed(
      form.allowedModels.includes(id)
        ? form.allowedModels.filter((item) => item !== id)
        : [...form.allowedModels, id],
    );
  }

  async function handleSubmit(): Promise<void> {
    const name = form.name.trim();
    if (!name) {
      toast.error(t('dashboard.projects.name_required', 'Give the project a name.'));
      return;
    }
    const input: ProjectInput = {
      name,
      daily_cost_quota_usd: form.daily.trim() === '' ? null : Number(form.daily),
      monthly_cost_quota_usd: form.monthly.trim() === '' ? null : Number(form.monthly),
      allowed_models: form.allowedModels,
    };
    try {
      if (projectId) {
        await updateMutation.mutateAsync({ id: projectId, input });
      } else {
        await createMutation.mutateAsync(input);
      }
      onClose();
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  const allSelected = modelIds.length > 0 && form.allowedModels.length === modelIds.length;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-4 sm:items-center">
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl bg-white p-6 shadow-xl">
        <h3 className="text-lg font-semibold text-gray-900">
          {projectId
            ? t('dashboard.projects.edit_title', 'Edit project')
            : t('dashboard.projects.create_title', 'New project')}
        </h3>
        <div className="mt-4 space-y-4">
          <label className="block">
            <span className="text-sm font-medium text-gray-700">
              {t('dashboard.projects.name', 'Name')}
            </span>
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
              placeholder={t('dashboard.projects.name_placeholder', 'e.g. research')}
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block">
              <span className="text-sm font-medium text-gray-700">
                {t('dashboard.projects.daily_budget', 'Daily budget (USD)')}
              </span>
              <input
                inputMode="decimal"
                value={form.daily}
                onChange={(event) => setForm({ ...form, daily: event.target.value })}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                placeholder={t('dashboard.projects.inherit', 'Account limit')}
              />
            </label>
            <label className="block">
              <span className="text-sm font-medium text-gray-700">
                {t('dashboard.projects.monthly_budget', 'Monthly budget (USD)')}
              </span>
              <input
                inputMode="decimal"
                value={form.monthly}
                onChange={(event) => setForm({ ...form, monthly: event.target.value })}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-500 focus:outline-none"
                placeholder={t('dashboard.projects.inherit', 'Account limit')}
              />
            </label>
          </div>
          <p className="text-xs text-gray-500">
            {t(
              'dashboard.projects.budget_hint',
              'A project budget is a sub-limit. The account budget is always the hard ceiling.',
            )}
          </p>

          <div>
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-700">
                {t('dashboard.projects.models', 'Models')}
              </span>
              <div className="flex gap-3 text-xs">
                <button
                  type="button"
                  className="text-gray-600 underline-offset-2 hover:underline"
                  onClick={() => setAllowed(allSelected ? [] : [...modelIds])}
                >
                  {allSelected
                    ? t('dashboard.projects.clear_all', 'Clear all')
                    : t('dashboard.projects.select_all', 'Select all')}
                </button>
              </div>
            </div>
            <p className="mt-1 text-xs text-gray-500">
              {t(
                'dashboard.projects.models_hint',
                'All models are selected by default. Uncheck the ones this project may not use.',
              )}
            </p>
            <div className="mt-2 grid max-h-52 gap-1 overflow-y-auto rounded-md border border-gray-200 p-2 sm:grid-cols-2">
              {modelIds.map((id) => {
                const checked = allSelected || form.allowedModels.includes(id);
                return (
                  <label
                    key={id}
                    className="flex items-center gap-2 rounded px-2 py-1 text-sm text-gray-700 hover:bg-gray-50"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        if (allSelected) {
                          setAllowed(modelIds.filter((modelId) => modelId !== id));
                        } else {
                          toggleModel(id);
                        }
                      }}
                    />
                    <span className="truncate">{id}</span>
                  </label>
                );
              })}
              {modelIds.length === 0 && (
                <span className="text-sm text-gray-500">
                  {t('dashboard.projects.no_models', 'No models available.')}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="subtle" onClick={onClose} disabled={isSaving}>
            {t('common.cancel', 'Cancel')}
          </Button>
          <Button onClick={() => void handleSubmit()} isLoading={isSaving}>
            {projectId ? t('common.save', 'Save') : t('dashboard.projects.create', 'Create project')}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ProjectsManager(): JSX.Element {
  const t = useT();
  const toast = useAccountToast();
  const { data: projects = [], isLoading } = useProjects();
  const { data: modelsResponse } = useModels();
  const deleteMutation = useDeleteProject();
  const [editing, setEditing] = useState<Project | null>(null);
  const [creating, setCreating] = useState(false);

  const modelIds = (modelsResponse?.data ?? []).map((model) => model.id);

  function initialFor(project: Project | null): FormState {
    if (!project) return EMPTY_FORM;
    return {
      name: project.name,
      daily: project.daily_cost_quota_usd ?? '',
      monthly: project.monthly_cost_quota_usd ?? '',
      allowedModels: project.allowed_models,
    };
  }

  async function handleDelete(project: Project): Promise<void> {
    const confirmed = window.confirm(
      t(
        'dashboard.projects.confirm_delete',
        'Delete this project? Its API keys will be revoked immediately.',
      ),
    );
    if (!confirmed) return;
    try {
      const result = await deleteMutation.mutateAsync(project.id);
      toast.success(
        t('dashboard.projects.deleted', 'Project deleted. {count} keys revoked.').replace(
          '{count}',
          String(result.keys_revoked),
        ),
      );
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold tracking-tight text-gray-900 sm:text-lg">
            {t('dashboard.projects.title', 'Projects')}
          </h2>
          <p className="mt-1 text-sm text-gray-600">
            {t(
              'dashboard.projects.subtitle',
              'Group keys under a shared budget and model allowlist. The account budget always applies.',
            )}
          </p>
        </div>
        <Button onClick={() => setCreating(true)} size="sm">
          {t('dashboard.projects.new', 'New project')}
        </Button>
      </div>

      {isLoading && (
        <div className="text-sm text-gray-500">{t('common.loading', 'Loading...')}</div>
      )}

      {!isLoading && projects.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-4 py-5 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
          {t(
            'dashboard.projects.empty',
            'No projects yet. Create one to give a set of keys a shared budget.',
          )}
        </div>
      )}

      <div className="space-y-3">
        {projects.map((project) => (
          <div
            key={project.id}
            className="rounded-lg border border-gray-200 p-4"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-medium text-gray-900">{project.name}</h3>
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">
                    {project.key_count} {t('dashboard.projects.keys', 'keys')}
                  </span>
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  {project.allowed_models.length === 0
                    ? t('dashboard.projects.all_models', 'All models allowed')
                    : `${project.allowed_models.length} ${t(
                        'dashboard.projects.models_allowed',
                        'models allowed',
                      )}`}
                </p>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="subtle" onClick={() => setEditing(project)}>
                  {t('common.edit', 'Edit')}
                </Button>
                <Button size="sm" variant="danger" onClick={() => void handleDelete(project)}>
                  {t('dashboard.projects.delete', 'Delete')}
                </Button>
              </div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <BudgetBar
                label={t('dashboard.projects.today', 'Today')}
                spent={project.spent_today_usd}
                limit={project.daily_cost_quota_usd}
              />
              <BudgetBar
                label={t('dashboard.projects.this_month', 'This month')}
                spent={project.spent_month_usd}
                limit={project.monthly_cost_quota_usd}
              />
            </div>
          </div>
        ))}
      </div>

      {creating && (
        <ProjectForm
          projectId={null}
          initial={EMPTY_FORM}
          modelIds={modelIds}
          onClose={() => setCreating(false)}
        />
      )}
      {editing && (
        <ProjectForm
          projectId={editing.id}
          initial={initialFor(editing)}
          modelIds={modelIds}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
