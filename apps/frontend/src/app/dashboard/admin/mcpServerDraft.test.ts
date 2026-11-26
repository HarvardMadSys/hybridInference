import { describe, expect, it } from 'vitest';

import { toDraft, validate, type Draft } from './mcpServerDraft';

function draft(headers: Draft['headers']): Draft {
  return {
    ...toDraft(),
    id: 'docs',
    name: 'Docs',
    url: 'https://docs.example/mcp',
    headers,
  };
}

describe('validate', () => {
  it('accepts an MCP_ secret reference', () => {
    expect(validate(draft([{ name: 'Authorization', value: '${MCP_DOCS_TOKEN}' }]))).toBeNull();
  });

  it('rejects an empty MCP_ name', () => {
    expect(validate(draft([{ name: 'Authorization', value: '${MCP_}' }]))).toContain(
      'must reference an MCP_ secret setting',
    );
  });

  it('rejects a reference outside the MCP_ namespace', () => {
    expect(validate(draft([{ name: 'Authorization', value: '${GITHUB_TOKEN}' }]))).toContain(
      'must reference an MCP_ secret setting',
    );
  });
});
