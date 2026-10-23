// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
let search = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/dashboard/admin/log',
  useSearchParams: () => search,
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  listAuditLog: vi.fn(),
}));

import { listAuditLog } from '@/lib/api/admin';
import { AuditTab } from './AuditTab';

const USER_ID = '0123456789abcdef0123';

beforeEach(() => {
  vi.clearAllMocks();
  search = new URLSearchParams();
  vi.mocked(listAuditLog).mockResolvedValue({
    total: 2,
    entries: [
      {
        id: 1,
        timestamp: '2026-10-22T10:00:00Z',
        admin_ip: '10.0.0.1',
        action: 'update_user',
        target_user_id: USER_ID,
        details: { role: 'pro' },
        success: true,
      },
      {
        id: 2,
        timestamp: '2026-10-22T11:00:00Z',
        admin_ip: '10.0.0.2',
        action: 'delete_key',
        target_user_id: null,
        details: null,
        success: false,
      },
    ],
  });
});

afterEach(cleanup);

describe('AuditTab', () => {
  it('labels its filters and shows each entry with its outcome and origin', async () => {
    render(<AuditTab />);

    expect(await screen.findByText('Update user')).toBeInTheDocument();
    expect(screen.getByLabelText('Filter by action')).toHaveValue('');
    expect(screen.getByLabelText('Filter by user ID')).toHaveValue('');
    expect(screen.getByText('2 entries')).toBeInTheDocument();
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.1')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.2')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Audit log' })).toBeInTheDocument();
  });

  it('filters by the target user and by action through the URL', async () => {
    render(<AuditTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Filter by user ${USER_ID}` }));
    expect(replace).toHaveBeenLastCalledWith(`/dashboard/admin/log?user_id=${USER_ID}`, {
      scroll: false,
    });

    fireEvent.change(screen.getByLabelText('Filter by action'), {
      target: { value: 'delete_key' },
    });
    expect(replace).toHaveBeenLastCalledWith('/dashboard/admin/log?action=delete_key', {
      scroll: false,
    });
  });
});
