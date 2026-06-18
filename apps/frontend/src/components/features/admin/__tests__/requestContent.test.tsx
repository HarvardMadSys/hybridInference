// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  JsonChatView,
  computePreview,
  flattenContent,
} from '@/components/features/admin/requestContent';

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

const anthropicMessages = [
  { role: 'user', content: [{ type: 'text', text: 'list the files' }] },
  {
    role: 'assistant',
    content: [
      { type: 'text', text: 'Let me check.' },
      { type: 'tool_use', id: 'toolu_abc', name: 'Bash', input: { command: 'ls -la' } },
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
];

describe('requestContent rendering', () => {
  it('renders Anthropic tool_use and tool_result blocks with full detail', () => {
    const { container } = render(<JsonChatView data={anthropicMessages} />);

    // Tool name and the literal argument JSON are visible (not just "[tool_use]").
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getAllByText('tool_use').length).toBeGreaterThan(0);
    expect(getSmallestMatchingElement(container, /"command": "ls -la"/)).toBeInTheDocument();

    // Tool result content is rendered, not collapsed to "[tool_result]".
    expect(screen.getAllByText('tool_result').length).toBeGreaterThan(0);
    expect(getSmallestMatchingElement(container, /file\.txt/)).toBeInTheDocument();

    expect(screen.queryByText('[tool_use]')).not.toBeInTheDocument();
    expect(screen.queryByText('[tool_result]')).not.toBeInTheDocument();
  });

  it('marks tool_result errors distinctly', () => {
    const { container } = render(
      <JsonChatView
        data={[
          {
            role: 'user',
            content: [
              { type: 'tool_result', tool_use_id: 'toolu_x', content: 'boom', is_error: true },
            ],
          },
        ]}
      />,
    );
    expect(getSmallestMatchingElement(container, /tool_result \(error\)/)).toBeInTheDocument();
    expect(getSmallestMatchingElement(container, /boom/)).toBeInTheDocument();
  });
});

describe('flattenContent', () => {
  it('skips tool_use / tool_result blocks but keeps text and other block types', () => {
    expect(
      flattenContent([
        { type: 'text', text: 'hello' },
        { type: 'tool_use', id: 't1', name: 'Bash', input: {} },
        { type: 'tool_result', tool_use_id: 't1', content: 'out' },
        { type: 'image' },
      ]),
    ).toBe('hello\n[image]');
  });
});

describe('computePreview', () => {
  it('falls back to a tool_results summary when the last user turn is only a tool_result', () => {
    // No text anywhere; without the fallback this would dump the raw JSON.
    const onlyToolResult = [
      {
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: 'toolu_zzz', content: '' }],
      },
    ];
    expect(computePreview(onlyToolResult, JSON.stringify(onlyToolResult))).toBe(
      '[tool_results: toolu_zzz]',
    );
  });

  it('surfaces Anthropic tool_use names in the assistant preview', () => {
    const data = [
      {
        role: 'assistant',
        content: [{ type: 'tool_use', id: 't2', name: 'Grep', input: {} }],
      },
    ];
    expect(computePreview(data, 'fallback')).toBe('[tool_calls: Grep]');
  });
});
