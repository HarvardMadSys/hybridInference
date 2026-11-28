// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TierFilter } from './TierFilter';

const COUNTS = { free: 10, pro: 2, internal: 1, admin: 4 };

afterEach(cleanup);

describe('TierFilter', () => {
  it('shows how many accounts are in each tier and selects one', () => {
    const onChange = vi.fn();
    render(<TierFilter value={null} counts={COUNTS} onChange={onChange} />);

    expect(screen.getByRole('group', { name: 'Account tiers' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Admin 4' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Admin 4' }));
    expect(onChange).toHaveBeenCalledWith('admin');
  });

  it('returns to every tier when the selected tier is chosen again', () => {
    const onChange = vi.fn();
    render(<TierFilter value="admin" counts={COUNTS} onChange={onChange} />);

    expect(screen.getByRole('button', { name: 'Admin 4' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Admin 4' }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('lists the tiers before their counts have loaded', () => {
    render(<TierFilter value={null} onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Free' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Free \d/ })).toBeNull();
  });

  it('lists service accounts next to Pro', () => {
    const onChange = vi.fn();
    render(<TierFilter value={null} counts={COUNTS} serviceAccountCount={3} onChange={onChange} />);

    const labels = screen.getAllByRole('button').map((b) => b.getAttribute('aria-label'));
    expect(labels.indexOf('Service 3')).toBe(labels.indexOf('Pro 2') + 1);

    fireEvent.click(screen.getByRole('button', { name: 'Service 3' }));
    expect(onChange).toHaveBeenCalledWith('service_account');
  });

  it('returns to every account when service accounts are chosen again', () => {
    const onChange = vi.fn();
    render(<TierFilter value="service_account" counts={COUNTS} onChange={onChange} />);

    const chip = screen.getByRole('button', { name: 'Service' });
    expect(chip).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
