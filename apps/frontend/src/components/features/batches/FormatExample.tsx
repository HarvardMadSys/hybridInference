'use client';

import { useState, type ReactNode } from 'react';
import toast from 'react-hot-toast';

import { Button } from '@/components/ui/Button';
import { useT } from '@/components/providers/useT';
import type { ConsoleTarget } from '@/lib/api/batches';
import { fill } from '@/lib/utils/interpolate';

import { saveText } from './errorReport';

/**
 * The smallest file of each type the console takes: `url` and `method` are
 * left out (the type is recognized from the body) and so is `model`, which
 * step 2 then asks for. What is shown is exactly what Copy and Download give.
 */
export const SAMPLES: Record<ConsoleTarget, { filename: string; lines: object[] }> = {
  chat: {
    filename: 'batch-text-generation-example.jsonl',
    lines: [
      {
        custom_id: 'request-1',
        body: { messages: [{ role: 'user', content: 'Summarize Hamlet in one sentence.' }] },
      },
      {
        custom_id: 'request-2',
        body: { messages: [{ role: 'user', content: 'Name three primary colors.' }] },
      },
    ],
  },
  embeddings: {
    filename: 'batch-embeddings-example.jsonl',
    lines: [
      { custom_id: 'doc-1', body: { input: 'The cat sat on the mat.' } },
      { custom_id: 'doc-2', body: { input: 'Batch jobs run in the background.' } },
    ],
  },
};

/** The sample as a file: UTF-8, one JSON object per line, LF line endings. */
export function sampleFile(type: ConsoleTarget): string {
  return `${SAMPLES[type].lines.map((line) => JSON.stringify(line)).join('\n')}\n`;
}

/** One compact JSON line, with keys and strings told apart. */
function highlight(json: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of json.matchAll(/("(?:[^"\\]|\\.)*")(:)?/g)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(json.slice(last, index));
    parts.push(
      <span key={index} className={match[2] ? 'text-purple-700' : 'text-emerald-700'}>
        {match[1]}
      </span>,
    );
    if (match[2]) parts.push(':');
    last = index + match[0].length;
  }
  if (last < json.length) parts.push(json.slice(last));
  return parts;
}

/** What a line of the file holds, and an example of each type to copy or download. */
export function FormatExample() {
  const t = useT();
  const [type, setType] = useState<ConsoleTarget>('chat');
  const sample = SAMPLES[type];
  const typeLabel = (value: ConsoleTarget) =>
    value === 'chat'
      ? t('batches.create.example.chat', 'Text generation')
      : t('batches.create.example.embeddings', 'Embeddings');

  async function copy() {
    try {
      await navigator.clipboard.writeText(sampleFile(type));
      toast.success(t('batches.create.example.copied', 'Example copied'));
    } catch {
      toast.error(
        t(
          'batches.create.example.copy_failed',
          'Could not copy: select the example and copy it yourself.',
        ),
      );
    }
  }

  return (
    <div className="space-y-2 text-sm text-gray-700">
      <p>
        <span className="font-medium text-gray-900">
          {t('batches.create.format.label', 'File format:')}
        </span>{' '}
        {t(
          'batches.create.format.summary',
          'one JSON request per line, each with a unique custom_id (1–512 characters) and the request in body.',
        )}{' '}
        {t(
          'batches.create.format.model',
          'If any line has no model, you choose one model for every request in step 2.',
        )}
      </p>
      <div className="overflow-hidden rounded-lg ring-1 ring-gray-200">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 bg-gray-50 px-2 py-1.5">
          <div
            className="inline-flex rounded-lg border border-gray-200 bg-white p-0.5"
            role="group"
            aria-label={t('batches.create.example.type', 'Example type')}
          >
            {(['chat', 'embeddings'] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={type === value}
                onClick={() => setType(value)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                  type === value ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100'
                }`}
              >
                {typeLabel(value)}
              </button>
            ))}
          </div>
          <div className="flex gap-1">
            <Button
              size="sm"
              variant="secondary"
              aria-label={t('batches.create.example.copy_label', 'Copy example')}
              onClick={() => void copy()}
            >
              {t('batches.create.example.copy', 'Copy')}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              aria-label={fill(t('batches.create.example.download_label', 'Download {file}'), {
                file: sample.filename,
              })}
              onClick={() => saveText(sample.filename, sampleFile(type), 'application/jsonl')}
            >
              {t('batches.create.example.download', 'Download')}
            </Button>
          </div>
        </div>
        <ol
          aria-label={fill(t('batches.create.example.lines', 'Example file: {type}'), {
            type: typeLabel(type),
          })}
          className="list-decimal divide-y divide-dashed divide-gray-200 bg-white py-1 pl-10 pr-3 font-mono text-xs leading-relaxed text-gray-800 marker:text-gray-400"
        >
          {sample.lines.map((line) => {
            const json = JSON.stringify(line);
            return (
              <li key={json} className="whitespace-pre-wrap break-words py-1 pl-1">
                {highlight(json)}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
