// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  listUserTags: vi.fn(),
  listModelVisibility: vi.fn(),
  createUserTag: vi.fn(),
  updateUserTag: vi.fn(),
  deleteUserTag: vi.fn(),
  previewUserTagRule: vi.fn(),
  runUserTagRule: vi.fn(),
}));

import {
  deleteUserTag,
  listModelVisibility,
  listUserTags,
  previewUserTagRule,
  runUserTagRule,
  updateUserTag,
  type UserTag,
} from '@/lib/api/admin';
import { UserTagsPanel, draftError, draftOf, draftWrite } from './UserTagsPanel';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const unattended: UserTag = {
  name: 'unattended',
  description: 'Sends requests around the clock.',
  system: true,
  quota_daily_cost: null,
  quota_monthly_cost: null,
  currency: 'USD',
  max_concurrent_requests: null,
  allowed_models: null,
  auto_enabled: true,
  auto_window_hours: 12,
  auto_min_active_hours: 12,
  manual_members: 1,
  auto_members: 3,
  dismissed_members: 1,
  created_at: '2026-11-27T00:00:00Z',
  updated_at: '2026-11-27T00:00:00Z',
};

const lab: UserTag = {
  ...unattended,
  name: 'lab-a',
  description: '',
  system: false,
  quota_daily_cost: 5,
  max_concurrent_requests: 2,
  allowed_models: ['model-a'],
  auto_enabled: false,
  manual_members: 2,
  auto_members: 0,
  dismissed_members: 0,
};

function renderPanel(onShowMembers = vi.fn(), activeTag: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(<UserTagsPanel activeTag={activeTag} onShowMembers={onShowMembers} />, { wrapper });
  return onShowMembers;
}

describe('the draft', () => {
  it('creates with every field and names the currency with an amount', () => {
    const draft = { ...draftOf(null), name: 'lab-b', quotaDaily: '2.5', maxConcurrent: '3' };
    expect(draftWrite(draft, null, 'USD')).toEqual({
      description: '',
      quota_daily_cost: 2.5,
      quota_monthly_cost: null,
      currency: 'USD',
      max_concurrent_requests: 3,
      allowed_models: null,
      auto_enabled: false,
      auto_window_hours: 12,
      auto_min_active_hours: 12,
    });
  });

  it('edits only what changed, and an emptied limit clears it', () => {
    const saved = draftOf(lab);
    expect(draftWrite({ ...saved, maxConcurrent: '' }, saved, 'USD')).toEqual({
      max_concurrent_requests: null,
    });
    expect(draftWrite({ ...saved, restrictModels: false }, saved, 'USD')).toEqual({
      allowed_models: null,
    });
    expect(draftWrite(saved, saved, 'USD')).toEqual({});
  });

  it.each([
    [{ name: 'Bad Name' }, true, /lowercase/],
    [{ quotaDaily: '-1' }, false, /daily quota/],
    [{ maxConcurrent: '0' }, false, /concurrent/],
    [{ restrictModels: true, allowedModels: [] }, false, /at least one model/],
    [{ windowHours: '200' }, false, /window/],
    [{ windowHours: '6', minActiveHours: '7' }, false, /at most its window/],
  ])('refuses %o', (patch, creating, message) => {
    expect(draftError({ ...draftOf(lab), ...patch }, creating)).toMatch(message);
  });
});

it('lists the tags with their members, and a tag shows its members in the table', async () => {
  vi.mocked(listUserTags).mockResolvedValue({ tags: [unattended, lab], currency: 'USD' });
  const onShowMembers = renderPanel();

  const chip = await screen.findByRole('button', { name: /unattended\s*4/ });
  fireEvent.click(chip);
  expect(onShowMembers).toHaveBeenCalledWith('unattended');
});

it('saves the limits an administrator sets on a tag', async () => {
  vi.mocked(listUserTags).mockResolvedValue({ tags: [unattended, lab], currency: 'USD' });
  vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  vi.mocked(updateUserTag).mockResolvedValue({ ...unattended, max_concurrent_requests: 2 });
  renderPanel();

  fireEvent.click(await screen.findByRole('button', { name: 'Manage tags' }));
  const row = screen.getByTestId('user-tag-unattended');
  expect(within(row).getByText('No limits')).toBeInTheDocument();
  expect(
    within(row).getByText('Automatic: requests in every one of the last 12 hours'),
  ).toBeInTheDocument();
  // A built-in tag cannot be deleted.
  expect(within(row).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();

  fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Daily quota'), { target: { value: '5' } });
  fireEvent.change(screen.getByLabelText('Max concurrent requests'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() =>
    expect(updateUserTag).toHaveBeenCalledWith('unattended', {
      quota_daily_cost: 5,
      currency: 'USD',
      max_concurrent_requests: 2,
    }),
  );
});

it('previews the accounts a changed rule matches before it is saved', async () => {
  vi.mocked(listUserTags).mockResolvedValue({ tags: [unattended], currency: 'USD' });
  vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  vi.mocked(previewUserTagRule).mockResolvedValue({
    window_hours: 6,
    min_active_hours: 5,
    truncated: false,
    matched: [
      {
        user_id: 'u1',
        email: 'bot@example.test',
        login_name: null,
        phone: null,
        user_name: null,
        status: 'active',
        active_hours: 6,
        member_source: null,
      },
    ],
  });
  renderPanel();

  fireEvent.click(await screen.findByRole('button', { name: 'Manage tags' }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Window hours'), { target: { value: '6' } });
  fireEvent.change(screen.getByLabelText('Active hours'), { target: { value: '5' } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }));

  expect(await screen.findByText('bot@example.test')).toBeInTheDocument();
  expect(previewUserTagRule).toHaveBeenCalledWith('unattended', {
    windowHours: 6,
    minActiveHours: 5,
  });
});

it('runs a rule now and deletes a tag after confirming', async () => {
  vi.mocked(listUserTags).mockResolvedValue({ tags: [unattended, lab], currency: 'USD' });
  vi.mocked(runUserTagRule).mockResolvedValue({ added: ['u1', 'u2'] });
  vi.mocked(deleteUserTag).mockResolvedValue(undefined);
  renderPanel();

  fireEvent.click(await screen.findByRole('button', { name: 'Manage tags' }));
  fireEvent.click(
    within(screen.getByTestId('user-tag-unattended')).getByRole('button', {
      name: 'Run rule now',
    }),
  );
  await waitFor(() => expect(runUserTagRule).toHaveBeenCalledWith('unattended'));

  const labRow = screen.getByTestId('user-tag-lab-a');
  expect(
    within(labRow).getByText('≤ $5.00/day · ≤ 2 concurrent · only model-a'),
  ).toBeInTheDocument();
  fireEvent.click(within(labRow).getByRole('button', { name: 'Delete' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Delete tag' }));
  await waitFor(() => expect(deleteUserTag).toHaveBeenCalledWith('lab-a'));
});
