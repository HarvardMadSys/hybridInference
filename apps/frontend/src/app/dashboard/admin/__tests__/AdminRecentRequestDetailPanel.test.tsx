// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AdminRecentRequestItem } from '@/lib/api/admin';
import { AdminRecentRequestDetailPanel } from '../AdminRecentRequestDetailPanel';

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

function getSmallestMatchingElement(container: HTMLElement, pattern: RegExp): HTMLElement {
  const elements = [container, ...Array.from(container.querySelectorAll<HTMLElement>('*'))];
  const match = elements.find((element) => {
    const text = element.textContent ?? '';
    if (!pattern.test(text)) return false;
    return !Array.from(element.children).some((child) => pattern.test(child.textContent ?? ''));
  });

  if (!match) {
    throw new Error(`No smallest matching element found for pattern: ${pattern}`);
  }

  return match;
}

describe('AdminRecentRequestDetailPanel', () => {
  it('shows full request details without internal network fields', () => {
    const { container } = render(
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
    expect(screen.queryByText('req_1234567890abcdefghij…')).not.toBeInTheDocument();

    expect(
      getSmallestMatchingElement(
        container,
        /model claude-sonnet.*prov anthropic.*status 200.*time .*stream yes.*cost \$0\.02/i,
      ),
    ).toBeInTheDocument();
    expect(
      getSmallestMatchingElement(container, /lat 2\.2s.*ttft 700ms.*decode 42\.3 tok\/s/i),
    ).toBeInTheDocument();
    expect(
      getSmallestMatchingElement(
        container,
        /in\/out 1,200\s*\/\s*301.*reason\/total 64\s*\/\s*1,665.*cache r\/w 80\s*\/\s*20/i,
      ),
    ).toBeInTheDocument();
    expect(
      getSmallestMatchingElement(
        container,
        /user Ada Admin.*email ada@example\.com.*uid user_abc123456789.*sess sess_123/i,
      ),
    ).toBeInTheDocument();

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

  it('renders Anthropic tool_use and tool_result content blocks with full detail', () => {
    const prompt = JSON.stringify([
      { role: 'user', content: [{ type: 'text', text: 'list the files' }] },
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'Let me check.' },
          {
            type: 'tool_use',
            id: 'toolu_abc',
            name: 'Bash',
            input: { command: 'ls -la' },
          },
        ],
      },
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'toolu_abc',
            content: 'total 8\nfile.txt',
            is_error: false,
          },
        ],
      },
    ]);

    const { container } = render(
      <AdminRecentRequestDetailPanel
        req={makeAdminRequest({ request_surface: 'anthropic_messages' })}
        content={{
          prompt,
          reasoning_content: null,
          response: null,
          loading: false,
        }}
      />,
    );

    // Expand the folded Prompt section so the JSON chat view renders.
    container.querySelectorAll('details').forEach((d) => {
      d.open = true;
      fireEvent(d, new Event('toggle', { bubbles: true }));
    });

    // Tool name and the literal argument JSON are both visible (not just "[tool_use]").
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getAllByText('tool_use').length).toBeGreaterThan(0);
    expect(getSmallestMatchingElement(container, /"command": "ls -la"/)).toBeInTheDocument();

    // Tool result content is rendered, not collapsed to "[tool_result]".
    expect(screen.getAllByText('tool_result').length).toBeGreaterThan(0);
    expect(getSmallestMatchingElement(container, /file\.txt/)).toBeInTheDocument();
    expect(screen.queryByText('[tool_use]')).not.toBeInTheDocument();
    expect(screen.queryByText('[tool_result]')).not.toBeInTheDocument();
  });
});
