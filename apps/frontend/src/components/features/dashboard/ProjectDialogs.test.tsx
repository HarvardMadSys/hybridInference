// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Project } from '@/lib/api/user';
import { ProjectDialogs, projectHasLimits, projectSummary } from './ProjectDialogs';

const state = vi.hoisted(() => ({ create: vi.fn(), update: vi.fn(), archive: vi.fn() }));
vi.mock('@/lib/hooks', () => ({
  useModels: () => ({
    data: {
      object: 'list',
      data: [
        { id: 'model-one', name: 'Model One' },
        { id: 'model-two', name: 'Model Two' },
      ],
    },
    isLoading: false,
  }),
  useCreateProject: () => ({ mutateAsync: state.create, isPending: false }),
  useUpdateProject: () => ({ mutateAsync: state.update, isPending: false }),
  useArchiveProject: () => ({ mutateAsync: state.archive, isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));

const CI: Project = {
  id: 'proj_ci',
  name: 'CI',
  is_default: false,
  budget_daily_usd: 5,
  budget_monthly_usd: null,
  allowed_models: ['model-one'],
  max_concurrent_requests: 2,
  spent_today_usd: 1.25,
  spent_month_usd: 3,
  key_count: 1,
  created_at: '2026-11-01T00:00:00Z',
};

beforeEach(() => {
  state.create.mockReset().mockResolvedValue({});
  state.update.mockReset().mockResolvedValue({});
  state.archive.mockReset().mockResolvedValue({ keys_revoked: 1 });
});
afterEach(cleanup);

function openCreate(onClose = vi.fn()): HTMLElement {
  render(<ProjectDialogs dialog={{ mode: 'create' }} onClose={onClose} />);
  return screen.getByRole('dialog');
}

it('creates shared limits and leaves blank limits unset', async () => {
  const onClose = vi.fn();
  const dialog = openCreate(onClose);
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Evaluations' } });
  fireEvent.change(within(dialog).getByLabelText('Daily budget (USD)'), { target: { value: '0' } });
  fireEvent.click(within(dialog).getByLabelText('Only the models selected below'));
  fireEvent.click(within(dialog).getByRole('checkbox', { name: /Model One/ }));
  fireEvent.click(within(dialog).getByRole('checkbox', { name: /Model Two/ }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  await waitFor(() =>
    expect(state.create).toHaveBeenCalledWith({
      name: 'Evaluations',
      budget_daily_usd: 0,
      budget_monthly_usd: null,
      max_concurrent_requests: null,
      allowed_models: ['model-one', 'model-two'],
    }),
  );
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

it('sends null when every model is allowed', async () => {
  const dialog = openCreate();
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Everything' } });
  fireEvent.click(within(dialog).getByLabelText('Only the models selected below'));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Select all' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  await waitFor(() =>
    expect(state.create).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'Everything', allowed_models: null }),
    ),
  );
});

it('rejects an empty model selection instead of allowing every model', () => {
  const dialog = openCreate();
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Nothing' } });
  fireEvent.click(within(dialog).getByLabelText('Only the models selected below'));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Clear all' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  expect(state.create).not.toHaveBeenCalled();
  expect(screen.getByText('Pick at least one model, or allow all models.')).toBeInTheDocument();
});

it('rejects an invalid budget instead of making it unlimited', () => {
  const dialog = openCreate();
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Evaluations' } });
  fireEvent.change(within(dialog).getByLabelText('Daily budget (USD)'), {
    target: { value: 'bad' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  expect(state.create).not.toHaveBeenCalled();
  expect(
    screen.getByText('Enter non-negative budgets and a positive whole concurrency limit.'),
  ).toBeInTheDocument();
});

it('edits a project starting from its saved limits', async () => {
  render(<ProjectDialogs dialog={{ mode: 'edit', project: CI }} onClose={vi.fn()} />);
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByLabelText('Daily budget (USD)')).toHaveValue('5');
  expect(within(dialog).getByLabelText('Max concurrent requests')).toHaveValue('2');
  fireEvent.change(within(dialog).getByLabelText('Daily budget (USD)'), { target: { value: '' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
  await waitFor(() =>
    expect(state.update).toHaveBeenCalledWith({
      projectId: 'proj_ci',
      changes: {
        name: 'CI',
        budget_daily_usd: null,
        budget_monthly_usd: null,
        max_concurrent_requests: 2,
        allowed_models: ['model-one'],
      },
    }),
  );
});

it('archives only after confirming, saying the keys stop working', async () => {
  const onClose = vi.fn();
  render(<ProjectDialogs dialog={{ mode: 'archive', project: CI }} onClose={onClose} />);
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText(/Archive CI\? Its keys stop working/)).toBeInTheDocument();
  expect(state.archive).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
  await waitFor(() => expect(state.archive).toHaveBeenCalledWith('proj_ci'));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

it('sums up what a project spent against its budgets and what else it limits', () => {
  const t = (_slot: string, english: string) => english;
  expect(projectSummary(CI, t)).toBe(
    '$1.25 of $5.00 today · $3.00 this month · 1 model(s) · Up to 2 at once',
  );
  expect(projectHasLimits(CI)).toBe(true);
  expect(
    projectHasLimits({
      ...CI,
      budget_daily_usd: null,
      allowed_models: null,
      max_concurrent_requests: null,
    }),
  ).toBe(false);
});
