import { describe, expect, it } from 'vitest';

import { parseClientTool } from '@/components/features/admin/RequestsTab';

describe('parseClientTool', () => {
  it('returns null for empty / missing user agents', () => {
    expect(parseClientTool(null)).toBeNull();
    expect(parseClientTool(undefined)).toBeNull();
    expect(parseClientTool('')).toBeNull();
    expect(parseClientTool('   ')).toBeNull();
  });

  it('recognizes the official OpenAI Node SDK user agent (OpenAI/JS ...)', () => {
    // The Pi coding agent wraps the OpenAI Node SDK without overriding the
    // User-Agent, so it shows up as `OpenAI/JS x.y.z`. It must be labeled as the
    // openai-node SDK, not the bare `openai` catch-all.
    expect(parseClientTool('OpenAI/JS 4.104.0')).toBe('openai-node');
    expect(parseClientTool('openai/js 5.0.0')).toBe('openai-node');
    expect(parseClientTool('openai-node/4.0.0')).toBe('openai-node');
    expect(parseClientTool('openai/javascript 4.0.0')).toBe('openai-node');
  });

  it('still recognizes the official OpenAI Python SDK user agent', () => {
    expect(parseClientTool('OpenAI/Python 1.40.0')).toBe('openai-python');
    expect(parseClientTool('openai-python/1.0.0')).toBe('openai-python');
  });

  it('recognizes known coding tools', () => {
    expect(parseClientTool('claude-code/0.1.0')).toBe('claude-code');
    expect(parseClientTool('Kilo-Code/1.2.3')).toBe('kilo-code');
    expect(parseClientTool('cline/2.0.0')).toBe('cline');
    expect(parseClientTool('codex-cli/0.5.0')).toBe('codex');
  });

  it('recognizes OpenCode and Kilo Code by the agent they actually send', () => {
    // Neither matched before: OpenCode had no rule at all, and Kilo's LLM
    // requests identify as `kilo/...`, which the `kilo-code/` rule missed. Both
    // fell through to the leading-token fallback below.
    expect(parseClientTool('opencode/0.4.11')).toBe('opencode');
    expect(parseClientTool('opencode/latest/0.4.11/cli')).toBe('opencode');
    expect(parseClientTool('kilo/latest/1.9.0/cli')).toBe('kilo-code');
    // The marketing spelling keeps working.
    expect(parseClientTool('Kilo-Code/1.2.3')).toBe('kilo-code');
  });

  it('falls back to the leading token for unknown slash-delimited agents', () => {
    expect(parseClientTool('myagent/1.0.0')).toBe('myagent');
  });
});
