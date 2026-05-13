// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseTab } from './RoutewiseTab';

vi.mock('@/lib/api/admin', () => ({
  clearRouteWeight: vi.fn(),
  listRouteWeights: vi.fn(),
  listRoutewiseSettings: vi.fn(),
  setRouteWeight: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  clearRouteWeight,
  listRouteWeights,
  listRoutewiseSettings,
  setRouteWeight,
  updateRoutewiseSetting,
} from '@/lib/api/admin';

describe('RoutewiseTab', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads routewise settings and only renders routewise routes', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'fixed-model',
        strategy: 'fixed',
        endpoint_id: 'fixed-model:remote',
        provider: 'remote',
        base_url: 'https://fixed.example.test',
        yaml_weight: 2,
        override_weight: null,
        effective_weight: 2,
      },
      {
        model_id: 'routewise-model',
        strategy: 'routewise',
        endpoint_id: 'routewise-model:remote',
        provider: 'remote',
        base_url: 'https://routewise.example.test',
        yaml_weight: 3,
        override_weight: null,
        effective_weight: 3,
      },
    ]);
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'decision_rule',
          value: 'pd',
          value_type: 'str',
          default_value: 'pd',
          description: 'Decision rule used by Routewise.',
        },
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

    expect(await screen.findByRole('heading', { level: 2, name: 'Routewise Settings' })).toBeInTheDocument();
    expect(await screen.findByText('routewise-model')).toBeInTheDocument();
    expect(screen.getByText('routewise-model:remote')).toBeInTheDocument();
    expect(screen.queryByText('fixed-model')).not.toBeInTheDocument();
    expect(screen.queryByText('fixed-model:remote')).not.toBeInTheDocument();
    expect(screen.getByLabelText('decision_rule value')).toHaveValue('pd');
  });

  it('updates decision_rule through the select control', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([]);
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'decision_rule',
          value: 'pd',
          value_type: 'str',
          default_value: 'pd',
          description: 'Decision rule used by Routewise.',
        },
      ],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue({
      key: 'decision_rule',
      value: 'lapd',
      value_type: 'str',
      default_value: 'pd',
      description: 'Decision rule used by Routewise.',
    });

    render(<RoutewiseTab />);

    const select = await screen.findByLabelText('decision_rule value');
    fireEvent.change(select, { target: { value: 'lapd' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save decision_rule' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('decision_rule', 'lapd');
    });
    expect(await screen.findByDisplayValue('lapd')).toBeInTheDocument();
  });

  it('validates numeric routewise settings before saving', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([]);
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

  it('saves and clears routewise route weights', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'routewise-model',
        strategy: 'routewise',
        endpoint_id: 'routewise-model:remote',
        provider: 'remote',
        base_url: 'https://routewise.example.test',
        yaml_weight: 3,
        override_weight: 4,
        effective_weight: 4,
      },
    ]);
    vi.mocked(listRoutewiseSettings).mockResolvedValue({ settings: [] });
    vi.mocked(setRouteWeight).mockResolvedValue({
      model_id: 'routewise-model',
      strategy: 'routewise',
      endpoint_id: 'routewise-model:remote',
      provider: 'remote',
      base_url: 'https://routewise.example.test',
      yaml_weight: 3,
      override_weight: 6,
      effective_weight: 6,
    });
    vi.mocked(clearRouteWeight).mockResolvedValue({
      model_id: 'routewise-model',
      strategy: 'routewise',
      endpoint_id: 'routewise-model:remote',
      provider: 'remote',
      base_url: 'https://routewise.example.test',
      yaml_weight: 3,
      override_weight: null,
      effective_weight: 3,
    });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('Runtime weight for routewise-model:remote');
    const saveButton = screen.getByRole('button', {
      name: 'Save routewise-model:remote weight',
    });

    expect(saveButton).toBeDisabled();
    fireEvent.change(input, { target: { value: '6' } });

    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith('routewise-model', 'routewise-model:remote', 6);
    });

    await waitFor(() => {
      expect(input).toHaveValue(6);
    });
    expect(saveButton).toBeDisabled();

    const clearButton = await screen.findByRole('button', {
      name: 'Clear routewise-model:remote override',
    });
    fireEvent.click(clearButton);

    await waitFor(() => {
      expect(clearRouteWeight).toHaveBeenCalledWith('routewise-model', 'routewise-model:remote');
    });

    await waitFor(() => {
      expect(input).toHaveValue(3);
    });
    expect(screen.queryByRole('button', { name: 'Clear routewise-model:remote override' })).not.toBeInTheDocument();
  });

  it('keeps save disabled for unchanged or invalid runtime weights', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'routewise-model',
        strategy: 'routewise',
        endpoint_id: 'routewise-model:remote',
        provider: 'remote',
        base_url: 'https://routewise.example.test',
        yaml_weight: 3,
        override_weight: null,
        effective_weight: 3,
      },
    ]);
    vi.mocked(listRoutewiseSettings).mockResolvedValue({ settings: [] });

    render(<RoutewiseTab />);

    const input = await screen.findByLabelText('Runtime weight for routewise-model:remote');
    const saveButton = screen.getByRole('button', {
      name: 'Save routewise-model:remote weight',
    });

    expect(saveButton).toBeDisabled();

    fireEvent.change(input, { target: { value: '-1' } });
    expect(saveButton).toBeDisabled();

    fireEvent.change(input, { target: { value: '3' } });
    expect(saveButton).toBeDisabled();

    fireEvent.change(input, { target: { value: '3.5' } });
    expect(saveButton).toBeEnabled();
  });
});
