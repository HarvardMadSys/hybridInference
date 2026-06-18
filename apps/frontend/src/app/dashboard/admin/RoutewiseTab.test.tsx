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
          key: 'routewise_latency_min_samples',
          value: 5,
          value_type: 'int',
          default_value: 3,
          description: 'Minimum samples before latency is considered.',
          min: 1,
          max: 20,
        },
      ],
    });

    render(<RoutewiseTab />);

    expect(
      await screen.findByRole('heading', { level: 2, name: 'RouteWise Settings' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('RouteWise parameters')).toBeInTheDocument();
    expect(screen.getByLabelText('routewise_latency_min_samples value')).toHaveValue(5);
    expect(screen.queryByText('Runtime Weight')).not.toBeInTheDocument();
  });

  it('validates numeric routewise settings before saving', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_latency_min_samples',
          value: 5,
          value_type: 'int',
          default_value: 3,
          description: 'Minimum samples before latency is considered.',
          min: 1,
          max: 20,
        },
      ],
    });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('routewise_latency_min_samples value');
    fireEvent.change(input, { target: { value: '1.5' } });

    expect(await screen.findByRole('alert')).toHaveTextContent('Must be a whole number.');
    fireEvent.click(screen.getByRole('button', { name: 'Save routewise_latency_min_samples' }));
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('saves routewise runtime parameters', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_latency_min_samples',
          value: 5,
          value_type: 'int',
          default_value: 3,
          description: 'Minimum samples before latency is considered.',
          min: 1,
          max: 20,
        },
      ],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue({
      key: 'routewise_latency_min_samples',
      value: 8,
      value_type: 'int',
      default_value: 3,
      description: 'Minimum samples before latency is considered.',
      min: 1,
      max: 20,
    });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('routewise_latency_min_samples value');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save routewise_latency_min_samples' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('routewise_latency_min_samples', 8);
    });
    expect(input).toHaveValue(8);
  });
});
