// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ModelQueue } from '@/lib/api/batches';
import { ModelQueueTab } from './ModelQueueTab';

vi.mock('@/lib/api/batches', () => ({
  getModelQueue: vi.fn(),
}));

import { getModelQueue } from '@/lib/api/batches';

const NOW = 1_800_000_000;

function queue(overrides: Partial<ModelQueue> = {}): ModelQueue {
  return {
    configured: true,
    error: null,
    cluster: 'Cluster A',
    scheduler: 'slurm',
    generated_at: NOW,
    refreshed_at: NOW - 120,
    started_at: NOW - 86_400,
    poll_interval_seconds: 60,
    policy: {
      pending_idle_cancel_seconds: 86_400,
      running_idle_stop_seconds: 900,
      renew_before_seconds: 1200,
      node_avoid_seconds: 21_600,
    },
    models: [
      {
        model: 'Qwen/Qwen3-8B',
        state: 'idle',
        job_name: 'od-qwen3-8b',
        last_request_at: null,
        walltime_seconds: 3600,
        startup_estimate_seconds: 600,
        partitions: 'gpu',
        constraint: null,
        avoided_nodes: [],
        jobs: [],
        gateway_models: ['qwen3-8b'],
        batch_queued: 0,
        batch_running: 0,
      },
      {
        model: 'google/gemma-3-12b-it',
        state: 'serving',
        job_name: 'od-gemma3-12b',
        last_request_at: NOW - 30,
        walltime_seconds: 3600,
        startup_estimate_seconds: 600,
        partitions: 'gpu',
        constraint: null,
        avoided_nodes: [],
        jobs: [
          {
            job_id: '2002',
            state: 'RUNNING',
            reason: null,
            partition: 'gpu',
            nodes: 'node-3',
            submitted_at: NOW - 1800,
            started_at: NOW - 1500,
            expected_start_at: null,
            ends_at: NOW + 2100,
          },
        ],
        gateway_models: ['gemma-3-12b'],
        batch_queued: 0,
        batch_running: 4,
      },
      {
        model: 'meta-llama/Llama-3.1-8B-Instruct',
        state: 'queued',
        job_name: 'od-llama31-8b',
        last_request_at: NOW - 60,
        walltime_seconds: 3600,
        startup_estimate_seconds: 600,
        partitions: 'gpu_requeue',
        constraint: 'a100|h100',
        avoided_nodes: ['node-7'],
        jobs: [
          {
            job_id: '1001',
            state: 'PENDING',
            reason: 'Priority',
            partition: 'gpu_requeue',
            nodes: null,
            submitted_at: NOW - 7200,
            started_at: null,
            expected_start_at: NOW + 1800,
            ends_at: null,
          },
        ],
        gateway_models: ['llama-3.1-8b'],
        batch_queued: 125,
        batch_running: 0,
      },
    ],
    failed_nodes: [
      {
        node: 'node-7',
        jobs: ['od-llama31-8b'],
        last_failed_at: NOW - 600,
        avoided_until: NOW + 21_000,
        avoided_by_all: false,
      },
    ],
    ...overrides,
  };
}

describe('ModelQueueTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('says how to set the queue up while no address is configured', async () => {
    vi.mocked(getModelQueue).mockResolvedValue(queue({ configured: false, models: [] }));
    render(<ModelQueueTab />);

    expect(await screen.findByText('No model queue is configured')).toBeInTheDocument();
    expect(screen.getByText('MODEL_QUEUE_STATUS_URL')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Settings → Providers' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings',
    );
  });

  it('lists the waiting models first, with their jobs and waiting batch requests', async () => {
    vi.mocked(getModelQueue).mockResolvedValue(queue());
    render(<ModelQueueTab />);

    expect(
      await screen.findByRole('heading', { name: 'Cluster A model queue' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Queue last read 2 min ago.')).toBeInTheDocument();

    const [models] = screen.getAllByRole('table');
    const rows = within(models).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'queue-meta-llama/Llama-3.1-8B-Instruct',
      'queue-google/gemma-3-12b-it',
    ]);

    const llama = within(rows[0]);
    expect(llama.getByText('queued')).toBeInTheDocument();
    expect(llama.getByText('llama-3.1-8b')).toBeInTheDocument();
    expect(llama.getByText('1001')).toBeInTheDocument();
    expect(llama.getByText(/waiting: Priority/)).toBeInTheDocument();
    expect(llama.getByText('submitted 2 h ago · est. start in 30 min')).toBeInTheDocument();
    expect(llama.getByText('125 waiting')).toBeInTheDocument();
    expect(llama.getByText('Avoids node-7')).toBeInTheDocument();

    const gemma = within(rows[1]);
    expect(gemma.getByText('serving')).toBeInTheDocument();
    expect(gemma.getByText(/node-3/)).toBeInTheDocument();
    expect(gemma.getByText('started 25 min ago · walltime ends in 35 min')).toBeInTheDocument();
    expect(gemma.getByText('4 running')).toBeInTheDocument();

    // Idle models are named, not given rows.
    expect(screen.getByTestId('idle-models')).toHaveTextContent('Qwen/Qwen3-8B');
    // The headline figures.
    expect(screen.getByText('Batch requests waiting').nextSibling).toHaveTextContent('125');
    expect(screen.getByTestId('failed-node-7')).toHaveTextContent('od-llama31-8b');
    expect(
      screen.getByText(/a waiting job is cancelled after 1 d without a request/),
    ).toBeVisible();
  });

  it('reports a host that cannot be read, and reads again on Refresh', async () => {
    vi.mocked(getModelQueue).mockResolvedValueOnce(
      queue({
        error: 'The queue status address refused the key (HTTP 401).',
        models: [],
        failed_nodes: [],
      }),
    );
    render(<ModelQueueTab />);

    expect(
      await screen.findByText('The queue status address refused the key (HTTP 401).'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();

    vi.mocked(getModelQueue).mockResolvedValueOnce(queue());
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(getModelQueue).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('125 waiting')).toBeInTheDocument();
  });

  it('says when every model is idle', async () => {
    const idle = queue();
    idle.models = idle.models.map((model) => ({ ...model, state: 'idle', jobs: [] }));
    vi.mocked(getModelQueue).mockResolvedValue(idle);
    render(<ModelQueueTab />);

    expect(await screen.findByText('No job is serving or waiting')).toBeInTheDocument();
  });

  it('shows a failed load of the page itself', async () => {
    vi.mocked(getModelQueue).mockRejectedValue(new Error('Not Found'));
    render(<ModelQueueTab />);

    expect(await screen.findByText('Not Found')).toBeInTheDocument();
  });
});
