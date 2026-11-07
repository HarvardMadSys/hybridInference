// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/config', () => ({
  getLiveConfigStatus: vi.fn(),
}));

import { getLiveConfigStatus, type LiveConfigStatus as Status } from '@/lib/api/config';

import { LiveConfigStatus, describeInstance } from './LiveConfigStatus';

function instance(overrides: Record<string, unknown> = {}, report: Record<string, unknown> = {}) {
  return {
    instance_id: 'a1b2c3d4',
    this_process: false,
    build: 'abc123',
    host: 'backend-1',
    lifecycle: 'running',
    started_at: null,
    heartbeat_age_seconds: 3,
    stale: false,
    supports: true,
    status: {
      state: 'applied',
      epoch: 'e1',
      reconciled_revision: 42,
      checked_revision: 42,
      ...report,
    },
    ...overrides,
  };
}

function status(instances: ReturnType<typeof instance>[], active = true): Status {
  return {
    database: true,
    source: 'app_config',
    version: { epoch: 'e1', revision: 42 },
    this_instance: 'a1b2c3d4',
    stale_after_seconds: 60,
    read_ok: true,
    unknown_groups: [],
    groups: [
      {
        name: 'alerts.runtime',
        keys: ['SLACK_WEBHOOK_URL'],
        schema_version: 1,
        contract: active ? { active: true, schema_version: 1 } : null,
        coverage: {
          registered: instances.length,
          live: instances.length,
          converged: instances.filter((item) => item.status?.reconciled_revision === 42).length,
          applying: 0,
          failed: 0,
          unsupported: 0,
          dormant: 0,
          unknown: instances.filter((item) => item.stale).length,
          stopped: 0,
          verified: false,
        },
        instances,
        local: null,
      },
    ],
  };
}

beforeEach(() => {
  vi.mocked(getLiveConfigStatus).mockReset();
});

afterEach(() => {
  cleanup();
});

describe('LiveConfigStatus', () => {
  it('shows the saved version, the coverage and each process, and never claims coverage', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue(
      status([instance(), instance({ instance_id: 'e5f6', host: 'backend-2' })]),
    );
    render(<LiveConfigStatus groupName="alerts.runtime" />);
    expect(
      await screen.findByText('app_config / epoch e1 / revision 42', { exact: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByText('2 of 2 registered processes have applied revision 42', { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Coverage is not verified/)).toBeInTheDocument();
    expect(screen.getAllByText('Applied')).toHaveLength(2);
    expect(screen.getByText(/does not mean a message was delivered/)).toBeInTheDocument();
  });

  it('never counts a lost heartbeat as applied', () => {
    expect(describeInstance(instance({ stale: true }), 42, 'e1')).toEqual({
      label: 'Unknown — no heartbeat',
      tone: 'warn',
    });
    expect(describeInstance(instance({}, { reconciled_revision: 41 }), 42, 'e1').label).toBe(
      'Applying',
    );
    expect(describeInstance(instance({ supports: false }), 42, 'e1').label).toBe(
      'Unsupported build',
    );
    expect(describeInstance(instance({}, { epoch: 'old' }), 42, 'e1').label).toBe(
      'Not reported yet',
    );
    expect(describeInstance(instance({}, { state: 'failed' }), 42, 'e1').label).toMatch(
      /previous values in use/,
    );
    expect(describeInstance(instance({}, { state: 'dormant-frozen' }), 42, 'e1').label).toBe(
      'Frozen until restart',
    );
  });

  it('names an error code and cleanup trouble, never a value', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue(
      status([
        instance(
          {},
          {
            state: 'invalid',
            error_code: 'channels_conflict',
            error_keys: ['WECOM_ALERTS_WEBHOOK_URL'],
          },
        ),
        instance({ instance_id: 'x2' }, { cleanup_degraded: true, detail: { detector: 'paused' } }),
      ]),
    );
    render(<LiveConfigStatus groupName="alerts.runtime" />);
    expect(
      await screen.findByText(/error: channels_conflict; WECOM_ALERTS_WEBHOOK_URL/),
    ).toBeInTheDocument();
    expect(screen.getByText(/cleanup degraded; detector paused/)).toBeInTheDocument();
  });

  it('says when the group is not activated', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue(
      status([instance({}, { state: 'dormant', pending_keys: ['SLACK_WEBHOOK_URL'] })], false),
    );
    render(<LiveConfigStatus groupName="alerts.runtime" />);
    expect(
      await screen.findByText(/Not activated: saved changes apply after a restart/),
    ).toBeInTheDocument();
    expect(screen.getByText('Waiting for restart')).toBeInTheDocument();
    expect(screen.getByText(/differs from saved: SLACK_WEBHOOK_URL/)).toBeInTheDocument();
  });

  it('reports a status read failure', async () => {
    vi.mocked(getLiveConfigStatus).mockRejectedValue(new Error('offline'));
    render(<LiveConfigStatus groupName="alerts.runtime" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the status');
  });
});
