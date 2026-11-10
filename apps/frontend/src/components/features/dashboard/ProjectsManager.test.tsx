// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProjectsManager } from './ProjectsManager';

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
  useProjects: () => ({
    data: [
      {
        id: 'proj_default',
        name: 'Default',
        is_default: true,
        spent_today_usd: 0,
        spent_month_usd: 0,
        key_count: 0,
      },
    ],
  }),
  useCreateProject: () => ({ mutateAsync: state.create, isPending: false }),
  useUpdateProject: () => ({ mutateAsync: state.update, isPending: false }),
  useArchiveProject: () => ({ mutateAsync: state.archive, isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
beforeEach(() => {
  state.create.mockReset().mockResolvedValue({});
});
afterEach(cleanup);

it('creates shared limits and leaves blank limits unset', async () => {
  render(<ProjectsManager />);
  fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));
  const dialog = screen.getByRole('dialog');
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
});

it('sends null when every model is allowed', async () => {
  render(<ProjectsManager />);
  fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Everything' } });
  fireEvent.click(within(dialog).getByLabelText('Only the models selected below'));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Select all' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  await waitFor(() =>
    expect(state.create).toHaveBeenCalledWith({
      name: 'Everything',
      budget_daily_usd: null,
      budget_monthly_usd: null,
      max_concurrent_requests: null,
      allowed_models: null,
    }),
  );
});

it('rejects an empty model selection instead of allowing every model', () => {
  render(<ProjectsManager />);
  fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Nothing' } });
  fireEvent.click(within(dialog).getByLabelText('Only the models selected below'));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Clear all' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
  expect(state.create).not.toHaveBeenCalled();
  expect(screen.getByText('Pick at least one model, or allow all models.')).toBeInTheDocument();
});

it('rejects an invalid budget instead of making it unlimited', () => {
  render(<ProjectsManager />);
  fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));
  const dialog = screen.getByRole('dialog');
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

it('protects the Default project from archiving', () => {
  render(<ProjectsManager />);
  expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument();
});
