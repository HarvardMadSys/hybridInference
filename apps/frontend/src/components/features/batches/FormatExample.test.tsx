// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FormatExample, SAMPLES, sampleFile } from './FormatExample';
import { saveText } from './errorReport';

vi.mock('./errorReport', () => ({ saveText: vi.fn() }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: toast }));

const writeText = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
});
afterEach(cleanup);

function shownLines(): string[] {
  const list = screen.getByRole('list', { name: /Example file/ });
  return within(list)
    .getAllByRole('listitem')
    .map((item) => item.textContent ?? '');
}

describe('SAMPLES', () => {
  // The page says what a line needs; the example must be such a line, and one
  // that leaves the model to step 2, as the page says it may.
  for (const [type, sample] of Object.entries(SAMPLES)) {
    it(`${type}: one request per line, each with a unique custom_id and a body, no model`, () => {
      const lines = sampleFile(type as 'chat' | 'embeddings').split('\n');
      expect(lines.pop()).toBe('');
      expect(lines).toHaveLength(sample.lines.length);
      const ids = new Set<string>();
      for (const raw of lines) {
        const line = JSON.parse(raw);
        expect(Object.keys(line).sort()).toEqual(['body', 'custom_id']);
        expect(line.custom_id.length).toBeGreaterThanOrEqual(1);
        expect(line.custom_id.length).toBeLessThanOrEqual(512);
        ids.add(line.custom_id);
        expect(line.body).not.toHaveProperty('model');
        // The shape the gateway recognizes the type by when url is left out.
        if (type === 'chat') {
          expect(line.body).toHaveProperty('messages');
          expect(line.body).not.toHaveProperty('input');
        } else {
          expect(line.body).toHaveProperty('input');
          expect(line.body).not.toHaveProperty('messages');
        }
      }
      expect(ids.size).toBe(lines.length);
    });
  }
});

describe('FormatExample', () => {
  it('states the format in one place and says when a model must be chosen', () => {
    render(<FormatExample />);
    expect(screen.getByText(/one JSON request per line/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /If any line has no model, you choose one model for every request in step 2/,
      ),
    ).toBeInTheDocument();
    expect(document.querySelector('details')).toBeNull();
  });

  it('switches between the text generation and embeddings examples', () => {
    render(<FormatExample />);
    const chat = screen.getByRole('button', { name: 'Text generation' });
    const embeddings = screen.getByRole('button', { name: 'Embeddings' });
    expect(chat).toHaveAttribute('aria-pressed', 'true');
    expect(embeddings).toHaveAttribute('aria-pressed', 'false');
    expect(shownLines()).toEqual(SAMPLES.chat.lines.map((line) => JSON.stringify(line)));

    fireEvent.click(embeddings);
    expect(embeddings).toHaveAttribute('aria-pressed', 'true');
    expect(chat).toHaveAttribute('aria-pressed', 'false');
    expect(shownLines()).toEqual(SAMPLES.embeddings.lines.map((line) => JSON.stringify(line)));
  });

  it('copies exactly the lines shown, for the type shown', async () => {
    render(<FormatExample />);
    fireEvent.click(screen.getByRole('button', { name: 'Embeddings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Copy example' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith(`${shownLines().join('\n')}\n`);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Example copied'));
  });

  it('says so when the clipboard refuses', async () => {
    writeText.mockRejectedValue(new Error('denied'));
    render(<FormatExample />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy example' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('downloads the example shown as a .jsonl file', () => {
    render(<FormatExample />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Download batch-text-generation-example.jsonl' }),
    );
    expect(saveText).toHaveBeenLastCalledWith(
      'batch-text-generation-example.jsonl',
      `${shownLines().join('\n')}\n`,
      'application/jsonl',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Embeddings' }));
    fireEvent.click(
      screen.getByRole('button', { name: 'Download batch-embeddings-example.jsonl' }),
    );
    expect(saveText).toHaveBeenLastCalledWith(
      'batch-embeddings-example.jsonl',
      sampleFile('embeddings'),
      'application/jsonl',
    );
  });
});
