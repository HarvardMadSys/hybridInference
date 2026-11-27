// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  listUserTags: vi.fn(),
  addUserTagMembers: vi.fn(),
  removeUserTagMember: vi.fn(),
}));

import { addUserTagMembers, listUserTags, removeUserTagMember } from '@/lib/api/admin';
import type { ConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { chooseOption } from '@/test-utils/select';
import { UserTagsSection, tagLimitsSummary } from './UserTagsSection';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** A write flow that runs each write at once, as the table's does without a dialog. */
const confirmation = {
  busy: false,
  open: false,
  savingKey: null,
  dialog: null,
  request: vi.fn(() => true),
  perform: vi.fn(async ({ run }: { run: () => Promise<void> }) => {
    try {
      await run();
      return null;
    } catch (err) {
      return String(err);
    }
  }),
} as unknown as ConfirmAction;

const tag = (name: string) => ({
  name,
  description: '',
  system: false,
  quota_daily_cost: null,
  quota_monthly_cost: null,
  currency: 'USD' as const,
  max_concurrent_requests: null,
  allowed_models: null,
  auto_enabled: false,
  auto_window_hours: 12,
  auto_min_active_hours: 12,
  manual_members: 0,
  auto_members: 0,
  dismissed_members: 0,
  created_at: '2026-11-27T00:00:00Z',
  updated_at: '2026-11-27T00:00:00Z',
});

function renderSection(onChanged = vi.fn(async () => undefined)) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(
    <UserTagsSection
      userId="u1"
      memberships={[
        {
          tag: 'unattended',
          source: 'auto',
          assigned_at: '2026-11-27T00:00:00Z',
          assigned_by: null,
        },
      ]}
      limits={{
        quota_daily_cost: 5,
        quota_monthly_cost: null,
        max_concurrent_requests: 2,
        allowed_models: ['model-a'],
        currency: 'USD',
      }}
      confirmation={confirmation}
      onChanged={onChanged}
    />,
    { wrapper },
  );
  return onChanged;
}

it('says what the tags hold the account to', () => {
  expect(
    tagLimitsSummary(
      {
        quota_daily_cost: 5,
        quota_monthly_cost: null,
        max_concurrent_requests: 2,
        allowed_models: [],
        currency: 'USD',
      },
      (value) => `$${value}`,
    ),
  ).toBe('daily quota ≤ $5 · ≤ 2 concurrent requests · no model (the tags allow none in common)');
  expect(tagLimitsSummary(undefined, String)).toBe('');
});

it('shows the account tags and limits, and takes a tag away', async () => {
  vi.mocked(listUserTags).mockResolvedValue({ tags: [tag('unattended')], currency: 'USD' });
  vi.mocked(removeUserTagMember).mockResolvedValue({
    user_id: 'u1',
    previous_source: 'auto',
    dismissed: true,
  });
  const onChanged = renderSection();

  expect(screen.getByTestId('user-tag-limits')).toHaveTextContent(
    'Tag limits: daily quota ≤ $5.00 · ≤ 2 concurrent requests · only model-a',
  );
  fireEvent.click(screen.getByRole('button', { name: 'Remove unattended' }));
  await waitFor(() => expect(removeUserTagMember).toHaveBeenCalledWith('unattended', 'u1'));
  expect(onChanged).toHaveBeenCalled();
});

it('adds a tag the account does not carry', async () => {
  vi.mocked(listUserTags).mockResolvedValue({
    tags: [tag('unattended'), tag('lab-a')],
    currency: 'USD',
  });
  vi.mocked(addUserTagMembers).mockResolvedValue({ added: ['u1'], unchanged: [], unknown: [] });
  renderSection();

  await screen.findByRole('combobox', { name: 'Tag to add' });
  chooseOption('Tag to add', 'lab-a');
  fireEvent.click(screen.getByRole('button', { name: 'Add' }));
  await waitFor(() => expect(addUserTagMembers).toHaveBeenCalledWith('lab-a', ['u1']));
});
