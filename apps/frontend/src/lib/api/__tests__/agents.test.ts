// Tests for the agent-jobs API client.
//
// The interesting behaviour is all in the stream reader: frame parsing across
// chunk boundaries, cursor tracking, and reconnect-without-gap. Those are the
// things that silently lose a user's event log if they are wrong.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  connectGitHub,
  connectGitLab,
  disconnectAgentIntegration,
  followUpAgentJob,
  getAgentIntegrations,
  getAgentJobArtifact,
  getAgentJobThread,
  listAgentJobs,
  streamAgentJob,
} from '../agents';
import * as client from '../client';

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[index++]));
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function frame(id: number, eventType: string, payload: Record<string, unknown>): string {
  const data = JSON.stringify({
    id,
    attempt_id: 1,
    seq: id,
    event_type: eventType,
    payload,
    created_at: null,
  });
  return `id: ${id}\nevent: ${eventType}\ndata: ${data}\n\n`;
}

const FINISHED = 'event: job_finished\ndata: {"state":"succeeded","published_pr_url":"u"}\n\n';

describe('agents api', () => {
  let fetchWithAuth: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchWithAuth = vi.spyOn(client, 'fetchWithAuth');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists jobs', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ jobs: [{ id: 'ajob_1' }] }));
    const jobs = await listAgentJobs();
    expect(jobs).toHaveLength(1);
    expect(jobs[0].id).toBe('ajob_1');
  });

  it('loads source-control integrations', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ providers: [{ provider: 'github', configured: true, connected: false }] }),
    );

    const result = await getAgentIntegrations();

    expect(result.providers[0].provider).toBe('github');
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/integrations');
  });

  it.each([
    ['github', connectGitHub],
    ['gitlab', connectGitLab],
  ] as const)('completes a %s OAuth connection with its state', async (provider, connect) => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ provider, connected: true }));

    await connect('oauth-code', 'signed-state');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      `/v1/agent/integrations/${provider}/connect`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ code: 'oauth-code', state: 'signed-state' }),
      }),
    );
  });

  it('disconnects one source-control account', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));

    await disconnectAgentIntegration('gitlab', 'group/id');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/integrations/gitlab/connections/group%2Fid',
      { method: 'DELETE' },
    );
  });

  it('treats a missing artifact as absent rather than an error', async () => {
    // A job that changed nothing legitimately has no patch.
    fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'nope' }, 404));
    await expect(getAgentJobArtifact('ajob_1', 'patch')).resolves.toBeNull();
  });

  it('treats a missing thread as an old standalone job', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ detail: 'not found' }, 404));
    await expect(getAgentJobThread('ajob_old')).resolves.toBeNull();
  });

  it('queues a follow-up in the same conversation', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 'ajob_child', state: 'waiting' }));

    const child = await followUpAgentJob('ajob_parent', { prompt: 'please add a test' });

    expect(child.id).toBe('ajob_child');
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/ajob_parent/follow-ups',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ prompt: 'please add a test' }),
      }),
    );
  });

  it('delivers events and the terminal frame', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([frame(1, 'message', { text: 'a' }), frame(2, 'usage', { text: 'b' }), FINISHED]),
    );
    const events: string[] = [];
    const onFinished = vi.fn();
    await streamAgentJob('ajob_1', { onEvent: (e) => events.push(e.event_type), onFinished });
    expect(events).toEqual(['message', 'usage']);
    expect(onFinished).toHaveBeenCalledWith({ state: 'succeeded', published_pr_url: 'u' });
  });

  it('parses frames split across chunk boundaries', async () => {
    // The network decides where chunks end, not the frame format.
    const whole = frame(1, 'message', { text: 'split' }) + FINISHED;
    const cut = Math.floor(whole.length / 3);
    fetchWithAuth.mockResolvedValue(
      sseResponse([whole.slice(0, cut), whole.slice(cut, cut * 2), whole.slice(cut * 2)]),
    );
    const events: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => events.push(e.id) });
    expect(events).toEqual([1]);
  });

  it('resumes from the last id after a mid-stream drop, with no gap or duplicate', async () => {
    // First connection ends without a terminal frame — exactly what the
    // server's stream cap produces for a long-running job.
    fetchWithAuth
      .mockResolvedValueOnce(sseResponse([frame(1, 'message', {}), frame(2, 'message', {})]))
      .mockResolvedValueOnce(sseResponse([frame(3, 'message', {}), FINISHED]));

    const seen: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.id) });

    expect(seen).toEqual([1, 2, 3]);
    const secondUrl = fetchWithAuth.mock.calls[1][1] as string;
    expect(secondUrl).toContain('after=2');
  });

  it('starts from an explicit resume point', async () => {
    fetchWithAuth.mockResolvedValue(sseResponse([FINISHED]));
    await streamAgentJob('ajob_1', { onEvent: () => {}, lastEventId: 42 });
    expect(fetchWithAuth.mock.calls[0][1]).toContain('after=42');
  });

  it('ignores keepalive comment frames', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([': keepalive\n\n', frame(1, 'message', {}), FINISHED]),
    );
    const seen: number[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.id) });
    expect(seen).toEqual([1]);
  });

  it('drops an unparsable frame instead of ending the stream', async () => {
    // Losing one row beats losing the rest of the job.
    fetchWithAuth.mockResolvedValue(
      sseResponse(['id: 1\nevent: message\ndata: {not json\n\n', frame(2, 'usage', {}), FINISHED]),
    );
    const seen: string[] = [];
    await streamAgentJob('ajob_1', { onEvent: (e) => seen.push(e.event_type) });
    expect(seen).toEqual(['usage']);
  });

  it('stops immediately when aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    await streamAgentJob('ajob_1', { onEvent: () => {}, signal: controller.signal });
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });
});
