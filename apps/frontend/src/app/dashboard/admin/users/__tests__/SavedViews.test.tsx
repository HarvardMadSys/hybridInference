// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SavedViews } from '../SavedViews';
import { STORAGE_KEY } from '../hooks/useSavedViews';
import { DEFAULT_FILTER_STATE } from '../lib/filterTypes';

vi.mock('@/lib/hooks/useMoney', () => ({ useMoney: () => ({ currency: 'CNY' }) }));

const saved = {
  id: 'big-spenders',
  name: 'Big spenders',
  builtin: false,
  filterState: { ...DEFAULT_FILTER_STATE, provider: 'openai', minCostToday: 5 },
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(STORAGE_KEY, JSON.stringify([saved]));
});
afterEach(cleanup);

describe('SavedViews on a ledger that moved from USD to CNY', () => {
  it('shows a view saved under USD and applies it without its threshold', () => {
    const onApply = vi.fn();
    render(<SavedViews current={DEFAULT_FILTER_STATE} onApply={onApply} />);

    const button = screen.getByRole('button', { name: /^Big spenders/ });
    expect(button).toHaveTextContent('no USD thresholds');
    expect(button).toHaveAttribute(
      'title',
      'Saved while amounts were in USD: its spend thresholds are left out',
    );
    fireEvent.click(button);
    expect(onApply).toHaveBeenCalledWith({
      ...saved.filterState,
      minCostToday: null,
      minCostMonth: null,
    });
  });

  it('deletes it where it was saved', () => {
    render(<SavedViews current={DEFAULT_FILTER_STATE} onApply={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete view Big spenders' }));
    expect(screen.queryByRole('button', { name: /^Big spenders/ })).toBeNull();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')).toEqual([]);
  });
});
