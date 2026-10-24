// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  listBroadcasts: vi.fn(),
  getBroadcastDetail: vi.fn(),
  previewBroadcast: vi.fn(),
  sendTestBroadcastEmail: vi.fn(),
  createBroadcast: vi.fn(),
  cancelBroadcast: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

let adminEmail: string | null = 'ops@example.test';
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      isAuthenticated: true,
      loading: false,
      user: { id: 'admin-1', email: adminEmail, role: 'admin', is_admin: true },
    },
  }),
}));

import toast from 'react-hot-toast';
import {
  cancelBroadcast,
  createBroadcast,
  getBroadcastDetail,
  listBroadcasts,
  previewBroadcast,
  sendTestBroadcastEmail,
  type BroadcastListItem,
  type BroadcastPreviewResponse,
  type CreateBroadcastResponse,
} from '@/lib/api/admin';

import { BroadcastsTab } from './BroadcastsTab';

const SCHEDULED: BroadcastListItem = {
  id: 'b1',
  subject: 'November maintenance',
  status: 'scheduled',
  recipient_count: 42,
  scheduled_at: '2026-11-01T09:30:00Z',
  sent_at: null,
  created_by: 'admin',
  created_at: '2026-10-20T00:00:00Z',
};

function previewOf(count: number, subject = 'Hello'): BroadcastPreviewResponse {
  return {
    recipient_count: count,
    rendered_subject: subject,
    rendered_body_html: '<p>Hi</p>',
    rendered_body_text: 'Hi',
  };
}

const CREATED: CreateBroadcastResponse = {
  id: 'b2',
  status: 'scheduled',
  recipient_count: 12,
  scheduled_at: null,
};

const BASE_REQUEST = {
  template_key: null,
  template_vars: {},
  subject: 'Hello',
  body_markdown: 'Hi there',
  body_text: '',
  target_roles: ['free', 'internal', 'admin'],
  target_statuses: ['active'],
  min_spend_today_usd: null,
};

function dialog() {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string) {
  fireEvent.click(within(dialog()).getByRole('button', { name }));
}

async function renderComposed() {
  render(<BroadcastsTab />);
  await waitFor(() => expect(listBroadcasts).toHaveBeenCalled());
  fireEvent.change(screen.getByPlaceholderText('Email subject'), { target: { value: 'Hello' } });
  fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: 'Hi there' } });
}

describe('BroadcastsTab', () => {
  let nativeConfirm: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    adminEmail = 'ops@example.test';
    vi.mocked(listBroadcasts).mockResolvedValue({ total: 0, broadcasts: [] });
    vi.mocked(previewBroadcast).mockResolvedValue(previewOf(12));
    nativeConfirm = vi.fn(() => true);
    vi.stubGlobal('confirm', nativeConfirm);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('runs Preview & Count directly: no confirmation, nothing written', async () => {
    await renderComposed();
    fireEvent.click(screen.getByRole('button', { name: 'Preview & Count' }));
    expect(await screen.findByText(/12 recipient/)).toHaveTextContent(
      '12 recipients match your filters',
    );
    expect(vi.mocked(previewBroadcast).mock.calls).toEqual([[BASE_REQUEST]]);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(createBroadcast).not.toHaveBeenCalled();
    expect(sendTestBroadcastEmail).not.toHaveBeenCalled();
  });

  describe('send now', () => {
    it('confirms with the previewed count and timing; Cancel writes nothing; Confirm sends once', async () => {
      vi.mocked(createBroadcast).mockResolvedValue(CREATED);
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Preview & Count' }));
      await screen.findByText(/match your filters/);

      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));
      const review = await screen.findByRole('dialog', {
        name: 'Send this broadcast to 12 recipients now?',
      });
      expect(review).toHaveTextContent('Recipients12 matching right now');
      expect(review).toHaveTextContent('SubjectHello');
      expect(review).toHaveTextContent('Rolesfree, internal, admin');
      expect(review).toHaveTextContent('Statusesactive');
      expect(review).toHaveTextContent('DeliveryImmediately');
      expect(review).toHaveTextContent('cannot be recalled');
      // The fresh preview is reused rather than counted again.
      expect(previewBroadcast).toHaveBeenCalledTimes(1);
      expect(within(review).getByRole('button', { name: 'Cancel' })).toHaveFocus();
      expect(createBroadcast).not.toHaveBeenCalled();

      clickInDialog('Cancel');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(createBroadcast).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));
      clickInDialog('Send to 12 recipients');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(createBroadcast).mock.calls).toEqual([
        [{ ...BASE_REQUEST, scheduled_at: null }],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Broadcast queued');
      expect(listBroadcasts).toHaveBeenCalledTimes(2);
    });

    it('counts the recipients first when there is no preview for the current content', async () => {
      vi.mocked(previewBroadcast)
        .mockResolvedValueOnce(previewOf(12))
        .mockResolvedValueOnce(previewOf(7));
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Preview & Count' }));
      await screen.findByText(/match your filters/);

      // Changing the audience makes that count stale: it is hidden and recounted.
      fireEvent.click(screen.getByLabelText('free'));
      expect(screen.queryByText(/match your filters/)).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));

      const review = await screen.findByRole('dialog', {
        name: 'Send this broadcast to 7 recipients now?',
      });
      expect(review).toHaveTextContent('Rolesinternal, admin');
      expect(vi.mocked(previewBroadcast).mock.calls[1]).toEqual([
        { ...BASE_REQUEST, target_roles: ['internal', 'admin'] },
      ]);
      expect(createBroadcast).not.toHaveBeenCalled();
    });

    it('opens no confirmation when the recipient count cannot be computed', async () => {
      vi.mocked(previewBroadcast).mockRejectedValueOnce(new Error('subject is required'));
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));
      await waitFor(() => expect(toast.error).toHaveBeenCalledWith('subject is required'));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(createBroadcast).not.toHaveBeenCalled();
    });

    it('blocks a second Confirm and closing while sending', async () => {
      let finish!: (value: CreateBroadcastResponse) => void;
      vi.mocked(createBroadcast).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));
      await screen.findByRole('dialog');
      clickInDialog('Send to 12 recipients');

      expect(within(dialog()).getByRole('button', { name: 'Sending…' })).toBeDisabled();
      expect(within(dialog()).getByRole('button', { name: 'Cancel' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Send Now' })).toBeDisabled();
      fireEvent.click(within(dialog()).getByRole('button', { name: 'Sending…' }));
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(createBroadcast).toHaveBeenCalledTimes(1);

      finish(CREATED);
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(createBroadcast).toHaveBeenCalledTimes(1);
    });

    it('keeps the dialog with the error after a failure, and retries the same send', async () => {
      vi.mocked(createBroadcast)
        .mockRejectedValueOnce(new Error('Failed to register broadcast with scheduler'))
        .mockResolvedValueOnce(CREATED);
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Send Now' }));
      await screen.findByRole('dialog');
      clickInDialog('Send to 12 recipients');

      expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
        'Failed to register broadcast with scheduler',
      );
      expect(screen.getByPlaceholderText('Email subject')).toHaveValue('Hello');
      clickInDialog('Send to 12 recipients');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(createBroadcast).mock.calls).toEqual([
        [{ ...BASE_REQUEST, scheduled_at: null }],
        [{ ...BASE_REQUEST, scheduled_at: null }],
      ]);
    });
  });

  describe('schedule', () => {
    it('needs a time, then confirms the schedule and audience and sends its ISO time', async () => {
      vi.mocked(previewBroadcast).mockResolvedValue(previewOf(3));
      vi.mocked(createBroadcast).mockResolvedValue(CREATED);
      await renderComposed();
      fireEvent.click(screen.getByLabelText('Schedule for later'));
      expect(screen.getByRole('button', { name: 'Schedule' })).toBeDisabled();
      expect(screen.getByText('Choose a date and time to schedule the broadcast.')).toBeVisible();

      fireEvent.change(screen.getByLabelText('Scheduled send time'), {
        target: { value: '2026-11-01T09:30' },
      });
      fireEvent.change(screen.getByLabelText('Minimum spend today (USD)'), {
        target: { value: '5' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Schedule' }));

      const when = new Date('2026-11-01T09:30');
      const review = await screen.findByRole('dialog', {
        name: 'Schedule this broadcast for 3 recipients?',
      });
      expect(review).toHaveTextContent(`Delivery${when.toLocaleString()}`);
      expect(review).toHaveTextContent('Spend today (UTC)More than $5');
      expect(review).toHaveTextContent('can be cancelled from Send History');
      expect(createBroadcast).not.toHaveBeenCalled();

      clickInDialog('Schedule');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(createBroadcast).mock.calls).toEqual([
        [{ ...BASE_REQUEST, min_spend_today_usd: 5, scheduled_at: when.toISOString() }],
      ]);
      expect(toast.success).toHaveBeenCalledWith('Broadcast scheduled');
    });
  });

  it('keeps the spend validation: an invalid threshold blocks preview, test and send', async () => {
    await renderComposed();
    fireEvent.change(screen.getByLabelText('Minimum spend today (USD)'), {
      target: { value: '-1' },
    });
    expect(screen.getByText('Please enter a valid non-negative number.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Preview & Count' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send Test to Me' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send Now' })).toBeDisabled();
  });

  describe('test send', () => {
    it('names the recipient; Cancel sends nothing; Confirm sends one test', async () => {
      vi.mocked(sendTestBroadcastEmail).mockResolvedValue(undefined);
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Send Test to Me' }));
      const review = screen.getByRole('dialog', {
        name: 'Send a test email to ops@example.test?',
      });
      expect(review).toHaveTextContent('Only ops@example.test receives it');
      clickInDialog('Cancel');
      expect(sendTestBroadcastEmail).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Send Test to Me' }));
      clickInDialog('Send test');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(sendTestBroadcastEmail).mock.calls).toEqual([[BASE_REQUEST]]);
      expect(toast.success).toHaveBeenCalledWith('Test email sent to your address');
      expect(createBroadcast).not.toHaveBeenCalled();
    });

    it('says so when the account has no email, and shows the server refusal in place', async () => {
      adminEmail = null;
      vi.mocked(sendTestBroadcastEmail).mockRejectedValueOnce(
        new Error('Your account has no email address'),
      );
      await renderComposed();
      fireEvent.click(screen.getByRole('button', { name: 'Send Test to Me' }));
      const review = screen.getByRole('dialog', { name: 'Send a test email to your account?' });
      expect(review).toHaveTextContent('Your account has no email address');
      clickInDialog('Send test');
      expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
        'Your account has no email address',
      );
    });
  });

  describe('cancel scheduled delivery', () => {
    it('replaces window.confirm, names the broadcast, and cancels once on Confirm', async () => {
      vi.mocked(listBroadcasts).mockResolvedValue({ total: 1, broadcasts: [SCHEDULED] });
      vi.mocked(cancelBroadcast).mockResolvedValue(undefined);
      render(<BroadcastsTab />);
      const cancel = await screen.findByRole('button', {
        name: 'Cancel scheduled broadcast November maintenance',
      });
      fireEvent.click(cancel);

      const review = screen.getByRole('dialog', {
        name: 'Cancel the scheduled broadcast “November maintenance”?',
      });
      expect(review).toHaveTextContent('It will not be sent to its 42 recipients');
      expect(review).toHaveTextContent(new Date(SCHEDULED.scheduled_at!).toLocaleString());
      expect(nativeConfirm).not.toHaveBeenCalled();
      expect(getBroadcastDetail).not.toHaveBeenCalled();

      clickInDialog('Keep scheduled');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(cancelBroadcast).not.toHaveBeenCalled();

      fireEvent.click(cancel);
      clickInDialog('Cancel broadcast');
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(vi.mocked(cancelBroadcast).mock.calls).toEqual([['b1']]);
      expect(toast.success).toHaveBeenCalledWith('Broadcast cancelled');
      expect(listBroadcasts).toHaveBeenCalledTimes(2);
    });

    it('keeps the dialog with the error when the cancel fails', async () => {
      vi.mocked(listBroadcasts).mockResolvedValue({ total: 1, broadcasts: [SCHEDULED] });
      vi.mocked(cancelBroadcast).mockRejectedValueOnce(new Error('already sending'));
      render(<BroadcastsTab />);
      fireEvent.click(
        await screen.findByRole('button', {
          name: 'Cancel scheduled broadcast November maintenance',
        }),
      );
      clickInDialog('Cancel broadcast');
      expect(await within(dialog()).findByRole('alert')).toHaveTextContent('already sending');
      expect(cancelBroadcast).toHaveBeenCalledTimes(1);
    });
  });
});
