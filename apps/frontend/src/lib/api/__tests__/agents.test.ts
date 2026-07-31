// Tests for the agent-jobs API client.
//
// The interesting behaviour is all in the stream reader: frame parsing across
// chunk boundaries, cursor tracking, and reconnect-without-gap. Those are the
// things that silently lose a user's event log if they are wrong.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import {
  archiveAgentJob,
  connectGitHub,
  connectGitLab,
  createAgentTerminal,
  deleteAgentTerminal,
  disconnectAgentIntegration,
  followUpAgentJob,
  forkAgentJob,
  getAgentIntegrations,
  getAgentJobArtifact,
  getAgentJobFiles,
  getAgentJobGit,
  getAgentJobThread,
  listAgentTerminals,
  listAgentJobs,
  pinAgentJob,
  resizeAgentTerminal,
  restartAgentJob,
  restoreAgentJob,
  streamAgentTerminal,
  streamAgentJob,
  unpinAgentJob,
  writeAgentTerminalInput,
  writeAgentJobFile,
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
    expect(fetchWithAuth).toHaveBeenCalledWith(expect.any(String), '/v1/agent/jobs?limit=50');
  });

  it('lists archived jobs separately', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ jobs: [] }));

    await listAgentJobs(25, true);

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs?limit=25&archived=true',
    );
  });

  it('archives and restores a whole task conversation', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', archived: true, archived_at: '2026-07-29T12:00:00Z' }),
    );

    await archiveAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/archive',
      { method: 'POST' },
    );

    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', archived: false, archived_at: null }),
    );
    await restoreAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/archive',
      { method: 'DELETE' },
    );
  });

  it('pins and unpins a whole task conversation', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', pinned: true, pinned_at: '2026-07-29T12:00:00Z' }),
    );

    await pinAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/pin',
      { method: 'POST' },
    );

    fetchWithAuth.mockResolvedValue(
      jsonResponse({ thread_id: 'athr_1', pinned: false, pinned_at: null }),
    );
    await unpinAgentJob('job/one');
    expect(fetchWithAuth).toHaveBeenLastCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/pin',
      { method: 'DELETE' },
    );
  });

  it('forks a conversation from a settled turn', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 'ajob_fork', state: 'succeeded' }));

    const fork = await forkAgentJob('job/one');

    expect(fork.id).toBe('ajob_fork');
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/fork',
      { method: 'POST' },
    );
  });

  it('restarts a task with an edited initial prompt', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ id: 'ajob_restart', state: 'queued' }));

    const restarted = await restartAgentJob('job/one', { prompt: 'corrected direction' });

    expect(restarted.id).toBe('ajob_restart');
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/restart',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: 'corrected direction' }),
      },
    );
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

  it('browses a job workspace with an encoded relative path', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ path: 'src/a file.ts', kind: 'file', content: 'x', size: 1 }),
    );

    await getAgentJobFiles('job/one', 'src/a file.ts');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/files?path=src%2Fa%20file.ts',
    );
  });

  it('writes a live workspace file with an encoded path', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({ path: 'src/a file.ts', kind: 'file', content: 'updated' }),
    );

    await writeAgentJobFile('job/one', 'src/a file.ts', 'updated');

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/files?path=src%2Fa%20file.ts',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ content: 'updated' }) }),
    );
  });

  it('manages PTY sessions and loads live Git state', async () => {
    const terminal = {
      id: 'term/1',
      shell: 'zsh',
      state: 'running',
      cwd: '/workspace',
      rows: 24,
      cols: 80,
      last_seq: 0,
    };
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ terminals: [terminal] }))
      .mockResolvedValueOnce(jsonResponse(terminal))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        jsonResponse({ available: true, branch: 'agent/x', changes: [], patch: '', commits: [] }),
      );

    await expect(listAgentTerminals('job/one')).resolves.toEqual([terminal]);
    await createAgentTerminal('job/one');
    await writeAgentTerminalInput('job/one', 'term/1', 'λ\r');
    await resizeAgentTerminal('job/one', 'term/1', 30, 100);
    await deleteAgentTerminal('job/one', 'term/1');
    await getAgentJobGit('ajob_1');

    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      1,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals',
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ rows: 24, cols: 80 }),
      }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      3,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/input',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ data: 'zrsN' }) }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      4,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/resize',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ rows: 30, cols: 100 }) }),
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      5,
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1',
      { method: 'DELETE' },
    );
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      6,
      expect.any(String),
      '/v1/agent/jobs/ajob_1/git',
    );
  });

  it('chunks large terminal pastes on complete UTF-8 characters', async () => {
    fetchWithAuth.mockResolvedValue(new Response(null, { status: 204 }));
    const pasted = `${'a'.repeat(65_535)}λ🙂z`;

    await writeAgentTerminalInput('job-1', 'term-1', pasted);

    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    const decoded = fetchWithAuth.mock.calls.map((call: unknown[]) => {
      const init = call[2] as RequestInit;
      const encoded = (JSON.parse(String(init.body)) as { data: string }).data;
      const bytes = Uint8Array.from(Buffer.from(encoded, 'base64'));
      expect(bytes.byteLength).toBeLessThanOrEqual(64 * 1024);
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    });
    expect(decoded.join('')).toBe(pasted);
  });

  it('streams terminal output and exit frames from an explicit resume cursor', async () => {
    fetchWithAuth.mockResolvedValue(
      sseResponse([
        'event: reset\ndata: {"seq":8,"reason":"output_truncated"}\n\n',
        'event: output\ndata: {"seq":9,"data":"aGk="}\n\n',
        'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
      ]),
    );
    const onOutput = vi.fn();
    const onReset = vi.fn();
    const onExit = vi.fn();

    await streamAgentTerminal('job/one', 'term/1', {
      after: 7,
      onOutput,
      onReset,
      onExit,
    });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/jobs/job%2Fone/terminals/term%2F1/stream?after=7',
      { headers: { Accept: 'text/event-stream' }, signal: undefined },
    );
    expect(onReset).toHaveBeenCalledWith({ seq: 8, reason: 'output_truncated' });
    expect(onOutput).toHaveBeenCalledWith({ seq: 9, data: 'aGk=' });
    expect(onExit).toHaveBeenCalledWith({ seq: 10, exit_code: 0 });
  });

  it('resumes a dropped terminal stream without replaying output', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        sseResponse([
          'event: broken\ndata: {nope}\n\n',
          'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
        ]),
      )
      .mockResolvedValueOnce(
        sseResponse([
          'event: output\ndata: {"seq":8,"data":"YQ=="}\n\n',
          'event: output\ndata: {"seq":9,"data":"Yg=="}\n\n',
          'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
        ]),
      );
    const onOutput = vi.fn();

    await streamAgentTerminal('job-1', 'term-1', {
      after: 7,
      onOutput,
      onReset: vi.fn(),
      onExit: vi.fn(),
    });

    expect(onOutput.mock.calls.map(([event]) => event.seq)).toEqual([8, 9]);
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job-1/terminals/term-1/stream?after=8',
      expect.any(Object),
    );
  });

  it('advances the resume cursor when a truncated-output reset is received', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        sseResponse(['event: reset\ndata: {"seq":8,"reason":"output_truncated"}\n\n']),
      )
      .mockResolvedValueOnce(
        sseResponse([
          'event: output\ndata: {"seq":9,"data":"YQ=="}\n\n',
          'event: exit\ndata: {"seq":10,"exit_code":0}\n\n',
        ]),
      );
    const onReset = vi.fn();

    await streamAgentTerminal('job-1', 'term-1', {
      after: 7,
      onOutput: vi.fn(),
      onReset,
      onExit: vi.fn(),
    });

    expect(onReset).toHaveBeenCalledOnce();
    expect(fetchWithAuth).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      '/v1/agent/jobs/job-1/terminals/term-1/stream?after=8',
      expect.any(Object),
    );
  });

  it('keeps reconnecting with capped backoff until it is aborted', async () => {
    vi.useFakeTimers();
    try {
      fetchWithAuth.mockRejectedValue(new Error('offline'));
      const controller = new AbortController();
      const stream = streamAgentTerminal('job-1', 'term-1', {
        signal: controller.signal,
        onOutput: vi.fn(),
        onReset: vi.fn(),
        onExit: vi.fn(),
      });

      for (let attempt = 0; attempt < 8; attempt += 1) {
        await vi.advanceTimersToNextTimerAsync();
      }
      expect(fetchWithAuth.mock.calls.length).toBeGreaterThan(7);
      controller.abort();
      await vi.runAllTimersAsync();
      await expect(stream).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
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
