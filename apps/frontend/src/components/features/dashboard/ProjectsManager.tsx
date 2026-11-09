'use client';

import { useRef, useState } from 'react';
import type { Project, ProjectCreate, ProjectUpdate } from '@/lib/api/user';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useArchiveProject, useCreateProject, useProjects, useUpdateProject } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { ModelPicker } from './ModelPicker';
import { useT } from '@/components/providers/useT';
import { formatUsd } from '@/lib/utils/currency';
import { fill } from '@/lib/utils/interpolate';

type EditState = { mode: 'create' } | { mode: 'edit'; project: Project } | null;

function parseBudget(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function ProjectsManager(): JSX.Element {
  const t = useT();
  const toast = useAccountToast();
  const { data: projects, isLoading, error } = useProjects();
  const createMutation = useCreateProject();
  const updateMutation = useUpdateProject();
  const archiveMutation = useArchiveProject();

  const [edit, setEdit] = useState<EditState>(null);
  const [archiveTarget, setArchiveTarget] = useState<Project | null>(null);
  const [names, setNames] = useState({
    name: '',
    daily: '',
    monthly: '',
    concurrency: '',
    models: null as string[] | null,
  });
  const [formError, setFormError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  const busy = createMutation.isPending || updateMutation.isPending || archiveMutation.isPending;

  function openCreate(): void {
    setNames({ name: '', daily: '', monthly: '', concurrency: '', models: null });
    setFormError(null);
    setEdit({ mode: 'create' });
  }

  function openEdit(project: Project): void {
    setNames({
      name: project.name,
      daily: project.budget_daily_usd == null ? '' : String(project.budget_daily_usd),
      monthly: project.budget_monthly_usd == null ? '' : String(project.budget_monthly_usd),
      concurrency:
        project.max_concurrent_requests == null ? '' : String(project.max_concurrent_requests),
      models: project.allowed_models ?? null,
    });
    setFormError(null);
    setEdit({ mode: 'edit', project });
  }

  async function confirmEdit(): Promise<void> {
    if (!edit || submittingRef.current) return;
    const name = names.name.trim();
    if (!name) {
      setFormError(t('dashboard.projects.name_required', 'Name is required.'));
      return;
    }
    const invalidBudget = [names.daily, names.monthly].some(
      (value) => value.trim() && (!Number.isFinite(Number(value)) || Number(value) < 0),
    );
    const concurrency = names.concurrency.trim() ? Number(names.concurrency) : null;
    if (
      invalidBudget ||
      (concurrency !== null && (!Number.isInteger(concurrency) || concurrency < 1))
    ) {
      setFormError(
        t(
          'dashboard.projects.invalid_limits',
          'Enter non-negative budgets and a positive whole concurrency limit.',
        ),
      );
      return;
    }
    if (names.models !== null && names.models.length === 0) {
      setFormError(
        t('dashboard.projects.pick_a_model', 'Pick at least one model, or allow all models.'),
      );
      return;
    }
    const budget: ProjectUpdate = {
      name,
      budget_daily_usd: parseBudget(names.daily),
      budget_monthly_usd: parseBudget(names.monthly),
      max_concurrent_requests: names.concurrency.trim() ? Number(names.concurrency.trim()) : null,
      allowed_models: names.models === null ? null : [...names.models].sort(),
    };
    submittingRef.current = true;
    setFormError(null);
    try {
      if (edit.mode === 'create') {
        await createMutation.mutateAsync(budget as ProjectCreate);
        toast.success(t('dashboard.projects.created_toast', 'Project created.'));
      } else {
        await updateMutation.mutateAsync({ projectId: edit.project.id, changes: budget });
        toast.success(t('dashboard.projects.updated_toast', 'Project updated.'));
      }
      setEdit(null);
    } catch (err) {
      setFormError(getErrorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  }

  async function confirmArchive(): Promise<void> {
    if (!archiveTarget || submittingRef.current) return;
    submittingRef.current = true;
    setFormError(null);
    try {
      const result = await archiveMutation.mutateAsync(archiveTarget.id);
      toast.success(
        fill(t('dashboard.projects.archived_toast', 'Project archived. {count} key(s) revoked.'), {
          count: String(result.keys_revoked),
        }),
      );
      setArchiveTarget(null);
    } catch (err) {
      setFormError(getErrorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  }

  const rows = projects ?? [];

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold tracking-tight text-gray-900 sm:text-lg">
            {t('dashboard.projects.title', 'Projects')}
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            {t(
              'dashboard.projects.subtitle',
              'Group API keys under a shared budget and model allowlist. Keys without a project use your Default project.',
            )}
          </p>
        </div>
        {!isLoading && (
          <Button onClick={openCreate} disabled={busy}>
            {t('dashboard.projects.create', 'Create Project')}
          </Button>
        )}
      </div>

      {error && (
        <div className="mb-4 rounded-md bg-red-50 px-4 py-3 text-sm text-red-700 ring-1 ring-inset ring-red-200">
          {t('dashboard.projects.load_failed', 'Failed to load projects. Please try again later.')}
        </div>
      )}

      {isLoading && (
        <div className="mb-4 text-sm text-gray-500">{t('common.loading', 'Loading...')}</div>
      )}

      {!isLoading && !error && rows.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-4 py-5 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
          {t('dashboard.projects.empty', 'No projects yet.')}
        </div>
      )}

      {rows.length > 0 && (
        <div className="divide-y divide-gray-200 rounded-lg ring-1 ring-gray-200">
          {rows.map((project) => (
            <div
              key={project.id}
              className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
                  <span className="font-medium text-gray-900">{project.name}</span>
                  {project.is_default && (
                    <span className="rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">
                      {t('dashboard.projects.default_badge', 'Default')}
                    </span>
                  )}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  {fill(t('dashboard.projects.spend_today', '{amount} today'), {
                    amount: formatUsd(project.spent_today_usd),
                  })}
                  {' · '}
                  {fill(t('dashboard.projects.spend_month', '{amount} this month'), {
                    amount: formatUsd(project.spent_month_usd),
                  })}
                  {' · '}
                  {fill(t('dashboard.projects.key_count', '{count} key(s)'), {
                    count: String(project.key_count),
                  })}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  {project.budget_daily_usd == null
                    ? t('dashboard.projects.no_daily_budget', 'No daily budget')
                    : fill(t('dashboard.projects.daily_budget', 'Daily {amount}'), {
                        amount: formatUsd(project.budget_daily_usd),
                      })}
                  {' · '}
                  {project.budget_monthly_usd == null
                    ? t('dashboard.projects.no_monthly_budget', 'No monthly budget')
                    : fill(t('dashboard.projects.monthly_budget', 'Monthly {amount}'), {
                        amount: formatUsd(project.budget_monthly_usd),
                      })}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                <Button variant="secondary" onClick={() => openEdit(project)} disabled={busy}>
                  {t('common.edit', 'Edit')}
                </Button>
                {!project.is_default && (
                  <Button
                    variant="danger"
                    onClick={() => {
                      setFormError(null);
                      setArchiveTarget(project);
                    }}
                    disabled={busy}
                  >
                    {t('dashboard.projects.archive', 'Archive')}
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {edit && (
        <ConfirmDialog
          title={
            edit.mode === 'create'
              ? t('dashboard.projects.create', 'Create Project')
              : t('dashboard.projects.edit', 'Edit Project')
          }
          confirmLabel={t(
            edit.mode === 'create' ? 'dashboard.projects.create' : 'common.save',
            edit.mode === 'create' ? 'Create Project' : 'Save',
          )}
          cancelLabel={t('common.cancel', 'Cancel')}
          busyLabel={t('common.loading', 'Loading...')}
          busy={busy}
          error={formError}
          onConfirm={() => void confirmEdit()}
          onCancel={() => {
            if (!submittingRef.current) setEdit(null);
          }}
        >
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void confirmEdit();
            }}
          >
            <InputField
              label={t('dashboard.projects.name_label', 'Name')}
              value={names.name}
              onChange={(event) => setNames({ ...names, name: event.target.value })}
              maxLength={80}
              disabled={busy}
              autoComplete="off"
            />
            <div className="grid grid-cols-2 gap-3">
              <InputField
                label={t('dashboard.projects.daily_label', 'Daily budget (USD)')}
                value={names.daily}
                onChange={(event) => setNames({ ...names, daily: event.target.value })}
                inputMode="decimal"
                placeholder={t('dashboard.projects.no_limit', 'No limit')}
                disabled={busy}
              />
              <InputField
                label={t('dashboard.projects.monthly_label', 'Monthly budget (USD)')}
                value={names.monthly}
                onChange={(event) => setNames({ ...names, monthly: event.target.value })}
                inputMode="decimal"
                placeholder={t('dashboard.projects.no_limit', 'No limit')}
                disabled={busy}
              />
            </div>
            <InputField
              label={t('dashboard.projects.concurrency_label', 'Max concurrent requests')}
              value={names.concurrency}
              onChange={(event) => setNames({ ...names, concurrency: event.target.value })}
              inputMode="numeric"
              placeholder={t('dashboard.projects.no_limit', 'No limit')}
              disabled={busy}
            />
            <ModelPicker
              value={names.models}
              onChange={(models) => setNames({ ...names, models })}
              title={t('dashboard.projects.models_title', 'Models')}
              hint={t(
                'dashboard.projects.models_hint',
                'Other models answer requests from this project as if they did not exist, and its model list shows only these.',
              )}
              disabled={busy}
            />
            <button type="submit" className="hidden" tabIndex={-1} disabled={busy} />
          </form>
        </ConfirmDialog>
      )}

      {archiveTarget && (
        <ConfirmDialog
          title={t('dashboard.projects.archive', 'Archive')}
          confirmLabel={t('dashboard.projects.archive', 'Archive')}
          cancelLabel={t('common.cancel', 'Cancel')}
          busyLabel={t('common.loading', 'Loading...')}
          busy={busy}
          tone="danger"
          error={formError}
          onConfirm={() => void confirmArchive()}
          onCancel={() => {
            if (!submittingRef.current) setArchiveTarget(null);
          }}
        >
          <p>
            {fill(
              t(
                'dashboard.projects.archive_prompt',
                'Archive {name}? Its keys stop working immediately. Its usage history stays available.',
              ),
              { name: archiveTarget.name },
            )}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
