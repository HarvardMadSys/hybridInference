'use client';

import { useRef, useState } from 'react';
import type { Project, ProjectCreate, ProjectUpdate } from '@/lib/api/user';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useArchiveProject, useCreateProject, useUpdateProject } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { InputField } from '@/components/ui/InputField';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { ModelPicker } from './ModelPicker';
import { useT } from '@/components/providers/useT';
import { useMoney } from '@/lib/hooks/useMoney';
import { formatMoney, moneyAmount, moneyCurrency } from '@/lib/utils/money';
import { fill } from '@/lib/utils/interpolate';

/** Which project dialog is open: a new project, one project's limits, or archiving one. */
export type ProjectDialog =
  | { mode: 'create' }
  | { mode: 'edit'; project: Project }
  | { mode: 'archive'; project: Project };

type Translate = ReturnType<typeof useT>;
/** Formats one of a project's money fields; `useMoney` provides the console's. */
type Money = Pick<ReturnType<typeof useMoney>, 'value'>;

/** Whether a project limits anything: a budget, a concurrency cap or a model allowlist. */
export function projectHasLimits(project: Project): boolean {
  return (
    moneyAmount(project, 'budget_daily') != null ||
    moneyAmount(project, 'budget_monthly') != null ||
    project.max_concurrent_requests != null ||
    project.allowed_models != null
  );
}

/**
 * One line on what a project spent against its budgets, and what else it limits.
 *
 * Amounts are the project's own: its native ledger amounts with its `currency`
 * when it has them, else its USD fields. `money` adds the console's locale.
 */
export function projectSummary(project: Project, t: Translate, money?: Money): string {
  const amount = (field: string) =>
    money
      ? money.value(project, field)
      : formatMoney(moneyAmount(project, field), moneyCurrency(project));
  const today =
    moneyAmount(project, 'budget_daily') == null
      ? fill(t('dashboard.projects.spend_today', '{amount} today'), {
          amount: amount('spent_today'),
        })
      : fill(t('dashboard.projects.spend_today_of', '{amount} of {budget} today'), {
          amount: amount('spent_today'),
          budget: amount('budget_daily'),
        });
  const month =
    moneyAmount(project, 'budget_monthly') == null
      ? fill(t('dashboard.projects.spend_month', '{amount} this month'), {
          amount: amount('spent_month'),
        })
      : fill(t('dashboard.projects.spend_month_of', '{amount} of {budget} this month'), {
          amount: amount('spent_month'),
          budget: amount('budget_monthly'),
        });
  const parts = [today, month];
  if (project.allowed_models != null) {
    parts.push(
      fill(t('dashboard.projects.models_count', '{count} model(s)'), {
        count: String(project.allowed_models.length),
      }),
    );
  }
  if (project.max_concurrent_requests != null) {
    parts.push(
      fill(t('dashboard.projects.concurrency_cap', 'Up to {count} at once'), {
        count: String(project.max_concurrent_requests),
      }),
    );
  }
  return parts.join(' · ');
}

function parseBudget(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function budgetText(value: number | null): string {
  return value == null ? '' : String(value);
}

/** The form a dialog opens with: budgets in the project's own (ledger) currency. */
function initialForm(project: Project | null) {
  return {
    name: project?.name ?? '',
    daily: budgetText(moneyAmount(project, 'budget_daily')),
    monthly: budgetText(moneyAmount(project, 'budget_monthly')),
    concurrency:
      project?.max_concurrent_requests == null ? '' : String(project.max_concurrent_requests),
    models: (project?.allowed_models ?? null) as string[] | null,
  };
}

function ProjectEditDialog({
  project,
  onClose,
}: {
  project: Project | null;
  onClose: () => void;
}): JSX.Element {
  const t = useT();
  const money = useMoney();
  // An existing project is edited in the currency its own response was read
  // in, so a USD-only response is never relabelled and saved as CNY; a new
  // project in the ledger currency the deployment reports (/site-config).
  const currency = project ? moneyCurrency(project) : money.currency;
  const native = currency !== 'USD';
  const toast = useAccountToast();
  const createMutation = useCreateProject();
  const updateMutation = useUpdateProject();
  const [form, setForm] = useState(() => initialForm(project));
  const [formError, setFormError] = useState<string | null>(null);
  const submittingRef = useRef(false);
  const busy = createMutation.isPending || updateMutation.isPending;
  const creating = project === null;

  async function confirm(): Promise<void> {
    if (submittingRef.current) return;
    const name = form.name.trim();
    if (!name) {
      setFormError(t('dashboard.projects.name_required', 'Name is required.'));
      return;
    }
    const invalidBudget = [form.daily, form.monthly].some(
      (value) => value.trim() && (!Number.isFinite(Number(value)) || Number(value) < 0),
    );
    const concurrency = form.concurrency.trim() ? Number(form.concurrency) : null;
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
    if (form.models !== null && form.models.length === 0) {
      setFormError(
        t('dashboard.projects.pick_a_model', 'Pick at least one model, or allow all models.'),
      );
      return;
    }
    // Budgets are typed and sent in the ledger currency: the native amount,
    // with its currency so the server refuses a stale page, on a non-USD
    // ledger; the `*_usd` field on a USD one.
    const budgets: ProjectUpdate = native
      ? {
          budget_daily: parseBudget(form.daily),
          budget_monthly: parseBudget(form.monthly),
          currency,
        }
      : {
          budget_daily_usd: parseBudget(form.daily),
          budget_monthly_usd: parseBudget(form.monthly),
        };
    const changes: ProjectUpdate = {
      name,
      ...budgets,
      max_concurrent_requests: concurrency,
      allowed_models: form.models === null ? null : [...form.models].sort(),
    };
    submittingRef.current = true;
    setFormError(null);
    try {
      if (creating) {
        await createMutation.mutateAsync(changes as ProjectCreate);
        toast.success(t('dashboard.projects.created_toast', 'Project created.'));
      } else {
        await updateMutation.mutateAsync({ projectId: project.id, changes });
        toast.success(t('dashboard.projects.updated_toast', 'Project updated.'));
      }
      onClose();
    } catch (err) {
      setFormError(getErrorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={
        creating
          ? t('dashboard.projects.create', 'Create Project')
          : t('dashboard.projects.edit', 'Edit Project')
      }
      confirmLabel={t(
        creating ? 'dashboard.projects.create' : 'common.save',
        creating ? 'Create Project' : 'Save',
      )}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={busy}
      error={formError}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submittingRef.current) onClose();
      }}
    >
      {creating && (
        <p>
          {t(
            'dashboard.projects.create_intro',
            'A project groups keys under one budget and model allowlist. Keys you create without picking a project go to Default.',
          )}
        </p>
      )}
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          void confirm();
        }}
      >
        <InputField
          label={t('dashboard.projects.name_label', 'Name')}
          value={form.name}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          maxLength={80}
          disabled={busy}
          autoComplete="off"
        />
        <div className="grid grid-cols-2 gap-3">
          <InputField
            label={fill(t('dashboard.projects.daily_label', 'Daily budget ({currency})'), {
              currency,
            })}
            value={form.daily}
            onChange={(event) => setForm({ ...form, daily: event.target.value })}
            inputMode="decimal"
            placeholder={t('dashboard.projects.no_limit', 'No limit')}
            disabled={busy}
          />
          <InputField
            label={fill(t('dashboard.projects.monthly_label', 'Monthly budget ({currency})'), {
              currency,
            })}
            value={form.monthly}
            onChange={(event) => setForm({ ...form, monthly: event.target.value })}
            inputMode="decimal"
            placeholder={t('dashboard.projects.no_limit', 'No limit')}
            disabled={busy}
          />
        </div>
        <InputField
          label={t('dashboard.projects.concurrency_label', 'Max concurrent requests')}
          value={form.concurrency}
          onChange={(event) => setForm({ ...form, concurrency: event.target.value })}
          inputMode="numeric"
          placeholder={t('dashboard.projects.no_limit', 'No limit')}
          disabled={busy}
        />
        <ModelPicker
          value={form.models}
          onChange={(models) => setForm({ ...form, models })}
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
  );
}

function ProjectArchiveDialog({
  project,
  onClose,
}: {
  project: Project;
  onClose: () => void;
}): JSX.Element {
  const t = useT();
  const toast = useAccountToast();
  const archiveMutation = useArchiveProject();
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  async function confirm(): Promise<void> {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setError(null);
    try {
      const result = await archiveMutation.mutateAsync(project.id);
      toast.success(
        fill(t('dashboard.projects.archived_toast', 'Project archived. {count} key(s) revoked.'), {
          count: String(result.keys_revoked),
        }),
      );
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={t('dashboard.projects.archive', 'Archive')}
      confirmLabel={t('dashboard.projects.archive', 'Archive')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={archiveMutation.isPending}
      tone="danger"
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submittingRef.current) onClose();
      }}
    >
      <p>
        {fill(
          t(
            'dashboard.projects.archive_prompt',
            'Archive {name}? Its keys stop working immediately. Its usage history stays available.',
          ),
          { name: project.name },
        )}
      </p>
    </ConfirmDialog>
  );
}

/** The open project dialog, if any. Each opening starts from the project as it is saved. */
export function ProjectDialogs({
  dialog,
  onClose,
}: {
  dialog: ProjectDialog | null;
  onClose: () => void;
}): JSX.Element | null {
  if (!dialog) return null;
  if (dialog.mode === 'archive') {
    return <ProjectArchiveDialog project={dialog.project} onClose={onClose} />;
  }
  const project = dialog.mode === 'edit' ? dialog.project : null;
  return <ProjectEditDialog key={project?.id ?? 'new'} project={project} onClose={onClose} />;
}
