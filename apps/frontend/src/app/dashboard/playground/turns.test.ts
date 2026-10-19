import { describe, expect, it } from 'vitest';
import { decodeRate, finishNote, requestHistory, type Message } from './turns';

let n = 0;
const user = (content: string): Message => ({ id: `u${n++}`, role: 'user', content });
const reply = (content: string, extra: Partial<Message> = {}): Message => ({
  id: `a${n++}`,
  role: 'assistant',
  content,
  ...extra,
});

describe('requestHistory', () => {
  it('keeps completed turns and the pending question', () => {
    expect(requestHistory([user('q1'), reply('a1'), user('q2')])).toEqual([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'a1' },
      { role: 'user', content: 'q2' },
    ]);
  });

  it.each([
    ['an error', reply('', { status: 'error', error: 'HTTP 502' })],
    ['an error after partial output', reply('half an ans', { status: 'error', error: 'reset' })],
    ['a stop before any output', reply('', { status: 'aborted' })],
    ['an empty reply', reply('   ')],
  ])('drops a turn that ended in %s, question included', (_case, failed) => {
    expect(requestHistory([user('q1'), failed, user('q2')])).toEqual([
      { role: 'user', content: 'q2' },
    ]);
  });

  it('keeps a reply that was stopped after it produced output', () => {
    expect(
      requestHistory([user('q1'), reply('partial', { status: 'aborted' }), user('q2')]),
    ).toEqual([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'partial' },
      { role: 'user', content: 'q2' },
    ]);
  });

  it('never sends display-only fields', () => {
    const [, assistant] = requestHistory([
      user('q'),
      reply('a', { reasoningContent: 'thoughts', modelName: 'GLM', durationMs: 5 }),
    ]);
    expect(Object.keys(assistant)).toEqual(['role', 'content']);
  });
});

describe('decodeRate', () => {
  it('measures tokens over the time after the first token', () => {
    // 100 tokens, 3s total of which 1s was TTFT: 50 tok/s, not 33.3.
    expect(decodeRate(reply('x', { completionTokens: 100, durationMs: 3000, ttftMs: 1000 }))).toBe(
      50,
    );
  });

  it('has no rate when the whole reply arrived in one burst', () => {
    // A buffering proxy delivers everything at once: 39 tokens in 1ms is not 39000 tok/s.
    expect(
      decodeRate(reply('x', { completionTokens: 39, durationMs: 1223, ttftMs: 1222 })),
    ).toBeNull();
  });

  it('has no rate without a token count or decode time', () => {
    expect(decodeRate(reply('x', { durationMs: 3000, ttftMs: 1000 }))).toBeNull();
    expect(
      decodeRate(reply('x', { completionTokens: 1, durationMs: 500, ttftMs: 500 })),
    ).toBeNull();
  });
});

describe('finishNote', () => {
  it('says a truncated answer hit the limit', () => {
    expect(finishNote(reply('partial', { finishReason: 'length' }))).toMatch(/Max Tokens limit/);
  });

  it('says the budget went on reasoning when no answer came', () => {
    expect(
      finishNote(reply('', { finishReason: 'length', reasoningContent: 'long thoughts' })),
    ).toMatch(/reasoning and never answered/);
  });

  it('says nothing for a normal stop', () => {
    expect(finishNote(reply('done', { finishReason: 'stop' }))).toBeNull();
  });
});
