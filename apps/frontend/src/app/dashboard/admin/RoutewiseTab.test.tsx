// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseTab } from './RoutewiseTab';

vi.mock('@/lib/api/admin', () => ({
  listRoutewiseSettings: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import { listRoutewiseSettings, updateRoutewiseSetting } from '@/lib/api/admin';

describe('RoutewiseTab', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads routewise runtime parameters without rendering endpoint weights', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
        {
          key: 'routewise_latency_slo_sec',
          value: 2.5,
          value_type: 'float',
          default_value: 3,
          description: 'Latency SLO in seconds for Routewise LP decisions.',
          min: 0.1,
          max: null,
        },
      ],
    });

    render(<RoutewiseTab />);

    expect(
      await screen.findByRole('heading', { level: 2, name: 'RouteWise Settings' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('RouteWise parameters')).toBeInTheDocument();
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.75);
    expect(screen.getByLabelText('Latency SLO (sec) value')).toHaveValue(2.5);
    expect(screen.queryByText('Runtime Weight')).not.toBeInTheDocument();
  });

  it('validates numeric routewise settings before saving', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
      ],
    });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(input, { target: { value: '1.5' } });

    expect(await screen.findByRole('alert')).toHaveTextContent('Must be ≤ 1.');
    fireEvent.click(screen.getByRole('button', { name: 'Save Cost budget alpha' }));
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('saves routewise runtime parameters', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
      ],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue({
      key: 'routewise_budget_alpha',
      value: 0.4,
      value_type: 'float',
      default_value: 0.75,
      description: 'RouteWise LP cost budget interpolation.',
      min: 0,
      max: 1,
    });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(input, { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Cost budget alpha' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('routewise_budget_alpha', 0.4);
    });
    expect(input).toHaveValue(0.4);
  });
});
