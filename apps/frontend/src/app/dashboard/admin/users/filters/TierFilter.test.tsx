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
});
