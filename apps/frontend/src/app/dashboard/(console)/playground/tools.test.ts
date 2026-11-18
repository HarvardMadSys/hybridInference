import { describe, expect, it } from 'vitest';

import type { HistoryMessage } from './turns';
import { formatHistory, resolveToolCall, selectedTools, wireName } from './tools';

const CALL = {
  id: 'call-1',
  name: 'search_web',
  mcpName: 'search web',
  arguments: '{"q":"x"}',
  status: 'done' as const,
  result: 'found it',
  serverUrl: 'https://mcp.example/mcp',
};

const HISTORY: HistoryMessage[] = [
  { role: 'user', content: 'q' },
  { role: 'assistant', content: '', toolCalls: [CALL] },
];

describe('wireName', () => {
  it('keeps valid characters and replaces the rest', () => {
    expect(wireName('search web')).toBe('search_web');
    expect(wireName('a.b/c')).toBe('a_b_c');
    expect(wireName('')).toBe('tool');
  });
});

describe('formatHistory', () => {
  it('builds chat tool_calls and tool results', () => {
    expect(formatHistory('chat', HISTORY)).toEqual([
      { role: 'user', content: 'q' },
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 'call-1',
            type: 'function',
            function: { name: 'search_web', arguments: '{"q":"x"}' },
          },
        ],
      },
      { role: 'tool', tool_call_id: 'call-1', content: 'found it' },
    ]);
  });

  it('builds Responses function_call and function_call_output items', () => {
    expect(formatHistory('responses', HISTORY)).toEqual([
      { role: 'user', content: 'q' },
      { type: 'function_call', call_id: 'call-1', name: 'search_web', arguments: '{"q":"x"}' },
      { type: 'function_call_output', call_id: 'call-1', output: 'found it' },
    ]);
  });

  it('builds Anthropic tool_use and tool_result blocks', () => {
    expect(formatHistory('messages', HISTORY)).toEqual([
      { role: 'user', content: 'q' },
      {
        role: 'assistant',
        content: [{ type: 'tool_use', id: 'call-1', name: 'search_web', input: { q: 'x' } }],
      },
      {
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: 'call-1', content: 'found it' }],
      },
    ]);
  });

  it('leaves plain messages and unresolved calls alone', () => {
    const plain: HistoryMessage[] = [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'a' },
    ];
    expect(formatHistory('chat', plain)).toEqual([
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'a' },
    ]);
    const unresolved: HistoryMessage[] = [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: '', toolCalls: [{ ...CALL, status: undefined }] },
    ];
    expect(formatHistory('messages', unresolved)).toEqual([
      { role: 'user', content: 'q' },
      { role: 'assistant', content: '' },
    ]);
  });
});

describe('selectedTools', () => {
  const servers = [
    {
      id: 's1',
      url: 'https://mcp.example/mcp',
      headers: { Authorization: 'Bearer x' },
      tools: [{ name: 'search', inputSchema: { type: 'object' } }],
    },
  ];

  it('resolves a selection to its server and definition', () => {
    expect(selectedTools(servers, [{ serverId: 's1', name: 'search' }])).toEqual([
      {
        serverId: 's1',
        serverUrl: 'https://mcp.example/mcp',
        serverHeaders: { Authorization: 'Bearer x' },
        tool: { name: 'search', inputSchema: { type: 'object' } },
        wire: 'search',
      },
    ]);
  });

  it('ignores a selection whose tool or server is gone', () => {
    expect(selectedTools(servers, [{ serverId: 's1', name: 'missing' }])).toEqual([]);
    expect(selectedTools([], [{ serverId: 's1', name: 'search' }])).toEqual([]);
  });

  it('attaches the server and the real tool name to a model call', () => {
    const selected = selectedTools(servers, [{ serverId: 's1', name: 'search' }]);
    expect(resolveToolCall({ id: 'c', name: 'search', arguments: '{}' }, selected)).toMatchObject({
      serverUrl: 'https://mcp.example/mcp',
      serverHeaders: { Authorization: 'Bearer x' },
      mcpName: 'search',
    });
  });

  it('gives same-named tools on different servers distinct wire names', () => {
    const two = [
      ...servers,
      {
        id: 's2',
        url: 'https://other.example/mcp',
        headers: {},
        tools: [{ name: 'search', inputSchema: { type: 'object' } }],
      },
    ];
    const selected = selectedTools(two, [
      { serverId: 's1', name: 'search' },
      { serverId: 's2', name: 'search' },
    ]);
    expect(selected.map((tool) => tool.wire)).toEqual(['search', 'search_2']);
    expect(resolveToolCall({ id: 'c', name: 'search_2', arguments: '{}' }, selected)).toMatchObject(
      { serverUrl: 'https://other.example/mcp', mcpName: 'search' },
    );
  });
});

describe('failed tool calls', () => {
  const failed = {
    ...CALL,
    status: 'error' as const,
    result: undefined,
    error: 'upstream rejected the arguments',
  };

  it('keeps the error content so the model can correct its arguments', () => {
    const history: HistoryMessage[] = [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: '', toolCalls: [failed] },
    ];
    expect(formatHistory('chat', history)).toMatchObject([
      {},
      {},
      { role: 'tool', tool_call_id: 'call-1', content: 'upstream rejected the arguments' },
    ]);
    expect(formatHistory('messages', history)).toMatchObject([
      {},
      {},
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'call-1',
            content: 'upstream rejected the arguments',
            is_error: true,
          },
        ],
      },
    ]);
  });
});
