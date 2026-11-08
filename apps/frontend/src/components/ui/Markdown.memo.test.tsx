// @vitest-environment jsdom
//
// How often a message's Markdown is parsed: once per text, not once per draw.
// Its own file, because it replaces the parser with one that counts.
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const parsed = vi.hoisted(() => ({ texts: [] as string[] }));

vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => {
    parsed.texts.push(children);
    return <p>{children}</p>;
  },
}));

import { Markdown } from './Markdown';

/** A conversation as the chat views draw one: every message, on every draw. */
function Conversation({ messages, draft }: { messages: string[]; draft: string }) {
  return (
    <>
      {messages.map((text, index) => (
        <Markdown key={index} text={text} />
      ))}
      <textarea value={draft} readOnly />
    </>
  );
}

beforeEach(() => {
  parsed.texts = [];
});

afterEach(cleanup);

describe('Markdown', () => {
  it('does not parse a message again when the conversation around it draws', () => {
    const { rerender } = render(<Conversation messages={['first', 'second']} draft="" />);

    // A keystroke in the input box.
    rerender(<Conversation messages={['first', 'second']} draft="h" />);
    // The next frame of a stream: only the newest message grew.
    rerender(<Conversation messages={['first', 'second', 'third']} draft="h" />);
    rerender(<Conversation messages={['first', 'second', 'third, longer']} draft="h" />);

    expect(parsed.texts).toEqual(['first', 'second', 'third', 'third, longer']);
  });

  it('parses again when the text or the tone changes', () => {
    const { rerender } = render(<Markdown text="a" />);

    rerender(<Markdown text="a" />);
    rerender(<Markdown text="ab" />);
    rerender(<Markdown text="ab" tone="dark" />);

    expect(parsed.texts).toEqual(['a', 'ab', 'ab']);
  });
});
