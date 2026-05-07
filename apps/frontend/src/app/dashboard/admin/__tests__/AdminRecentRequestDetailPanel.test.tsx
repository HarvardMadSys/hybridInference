// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AdminRecentRequestItem } from '@/lib/api/admin';
import { AdminRecentRequestDetailPanel } from '../page';

function makeAdminRequest(overrides: Partial<AdminRecentRequestItem> = {}): AdminRecentRequestItem {
  return {
    request_id: 'req_1234567890abcdefghijklmnopFULLID',
    user_id: 'user_abc123456789',
    user_name: 'Ada Admin',
    user_email: 'ada@example.com',
    user_ip: '203.0.113.10',
    peer_ip: '10.0.0.12',
    ip_source: 'x-forwarded-for',
    x_forwarded_for: '198.51.100.9, 10.0.0.12',
    user_agent: 'Claude-Code/1.0 long user agent value',
    session_id: 'sess_123',
    request_surface: 'openai_chat_completions',
    model_id: 'claude-sonnet',
    provider: 'anthropic',
    timestamp: '2026-05-06T12:00:00.000Z',
    status_code: 200,
    latency_ms: 2200,
    ttft_ms: 700,
    decode_throughput_tps: 42.25,
    stream: true,
    prompt_tokens: 1200,
    completion_tokens: 301,
    reasoning_tokens: 64,
    cache_read_tokens: 80,
    cache_write_tokens: 20,
    total_tokens: 1665,
    cost_usd: 0.0234,
    error: null,
    ...overrides,
  };
}

describe('AdminRecentRequestDetailPanel', () => {
  it('shows full request details without internal network fields', () => {
    render(
      <AdminRecentRequestDetailPanel
        req={makeAdminRequest()}
        content={{
          prompt: 'Prompt body',
          reasoning_content: 'Reasoning body',
          response: 'Response body',
          loading: false,
        }}
      />,
    );

    expect(screen.getByText('req_1234567890abcdefghijklmnopFULLID')).toBeInTheDocument();
    expect(screen.queryByText('req_1234567890abcdefghijkl...')).not.toBeInTheDocument();

    const performanceLine = screen.getByLabelText('Request performance and token details');
    expect(within(performanceLine).getByText('Latency')).toBeInTheDocument();
    expect(within(performanceLine).getByText('2.2s')).toBeInTheDocument();
    expect(within(performanceLine).getByText('TTFT')).toBeInTheDocument();
    expect(within(performanceLine).getByText('700ms')).toBeInTheDocument();
    expect(within(performanceLine).getByText('Decode')).toBeInTheDocument();
    expect(within(performanceLine).getByText('42.3 tok/s')).toBeInTheDocument();

    expect(screen.getByText('User agent')).toBeInTheDocument();
    expect(screen.getByText('Claude-Code/1.0 long user agent value')).toBeInTheDocument();

    expect(screen.queryByText('Network details')).not.toBeInTheDocument();
    expect(screen.queryByText('Peer IP:')).not.toBeInTheDocument();
    expect(screen.queryByText('IP source:')).not.toBeInTheDocument();
    expect(screen.queryByText('X-Forwarded-For:')).not.toBeInTheDocument();
    expect(screen.queryByText('10.0.0.12')).not.toBeInTheDocument();
    expect(screen.queryByText('x-forwarded-for')).not.toBeInTheDocument();
    expect(screen.queryByText('198.51.100.9, 10.0.0.12')).not.toBeInTheDocument();

    const prompt = screen.getByText('Prompt');
    const reasoning = screen.getByText('Reasoning');
    const response = screen.getByText('Response');
    expect(
      prompt.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      reasoning.compareDocumentPosition(response) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
