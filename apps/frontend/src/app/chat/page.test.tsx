// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type StreamOptions = {
  onToken: (delta: string) => void;
  onSources: (sources: unknown[]) => void;
  signal: AbortSignal;
};

const stream = vi.hoisted(() => ({
  options: null as StreamOptions | null,
  finish: () => undefined as void,
}));
const drawn = vi.hoisted(() => ({ texts: [] as string[] }));

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'Console', docsUrl: '' }),
  useSiteConfig: () => ({ features: { rag: true } }),
}));
vi.mock('@/lib/api/chat', () => ({
  streamRagChat: (options: StreamOptions) =>
    new Promise<void>((resolve) => {
      stream.options = options;
      stream.finish = resolve;
    }),
}));
// Each time the reply's Markdown is drawn, and with what.
vi.mock('@/components/ui/Markdown', () => ({
  Markdown: ({ text }: { text: string }) => {
    drawn.texts.push(text);
    return <div data-testid="reply">{text}</div>;
  },
}));

import ChatPage from './page';

let frames: FrameRequestCallback[];

function nextFrame() {
  act(() => {
    for (const callback of frames.splice(0)) callback(performance.now());
  });
}

async function ask(question: string) {
  render(<ChatPage />);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: question } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  });
}

beforeEach(() => {
  frames = [];
  drawn.texts = [];
  stream.options = null;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    frames[handle - 1] = () => undefined;
  });
  Element.prototype.scrollTo = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the docs assistant', () => {
  // Drawing on every token parsed the whole reply again for each one.
  it('draws the streaming reply once per frame, with every token that arrived in it', async () => {
    await ask('How do I get a key?');

    act(() => {
      for (const token of ['Open ', 'the ', 'dashboard']) stream.options?.onToken(token);
    });
    expect(drawn.texts).toEqual([]);

    nextFrame();
    expect(drawn.texts).toEqual(['Open the dashboard']);

    act(() => stream.options?.onToken('.'));
    nextFrame();
    expect(drawn.texts).toEqual(['Open the dashboard', 'Open the dashboard.']);
  });

  it('draws the tokens of the last frame when the stream ends before it', async () => {
    await ask('How do I get a key?');

    act(() => stream.options?.onToken('Done'));
    await act(async () => stream.finish());

    expect(screen.getByTestId('reply')).toHaveTextContent('Done');
    // The frame that was pending draws nothing more.
    nextFrame();
    expect(drawn.texts.at(-1)).toBe('Done');
  });
});
