// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import { ConfigEntryField } from './ConfigEntryField';
import { buildPatch, draftError, type ConfigDraft } from './configDrafts';

const CATALOG = JSON.stringify({
  models: [
    {
      id: 'example-chat',
      context_length: 4096,
      supports_tools: false,
      supported_params: ['temperature', 'top_p'],
      route: [{ base_url: '${EXAMPLE_UPSTREAM_BASE_URL}', weight: 1 }],
    },
  ],
});

function documentEntry(overrides: Partial<ConfigEntry> = {}): ConfigEntry {
  return {
    key: 'MODEL_CATALOG',
    category: 'models',
    description: 'Model catalog as JSON.',
    type: 'text',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: CATALOG,
    default: null,
    source: 'database',
    restart_required: true,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: true,
    one_time: true,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    document: true,
    ...overrides,
  };
}

/** The draft the field last reported, as the settings page would keep it. */
let latest: ConfigDraft | undefined;

function Harness({ entry, serverError }: { entry: ConfigEntry; serverError?: string }) {
  const [draft, setDraft] = useState<ConfigDraft | undefined>();
  latest = draft;
  return (
    <>
      <ConfigEntryField
        entry={entry}
        draft={draft}
        onChange={(_key, next) => setDraft(next)}
        serverError={serverError}
      />
      <button type="button" onClick={() => setDraft(undefined)}>
        Discard
      </button>
    </>
  );
}

function renderDocument(entry = documentEntry(), serverError?: string) {
  latest = undefined;
  return render(<Harness entry={entry} serverError={serverError} />);
}

const field = (label: string) => screen.getByRole('textbox', { name: label });
const type = (label: string, value: string) =>
  fireEvent.change(field(label), { target: { value } });
const draftDocument = () => JSON.parse(String(latest && latest.kind === 'value' && latest.value));

afterEach(() => {
  cleanup();
});

describe('a JSON document as fields', () => {
  it('follows a newer saved document while nothing is unsaved here, and edits build on it', () => {
    // A conflict reload, another key's save, a restore: the page takes the
    // newer configuration while this document has no draft.
    const { rerender } = renderDocument();
    const newer = JSON.parse(CATALOG);
    newer.models[0].context_length = 8192;
    rerender(<Harness entry={documentEntry({ value: JSON.stringify(newer) })} />);

    expect(field('models[0].context_length')).toHaveValue('8192');
    type('models[0].id', 'renamed');
    expect(draftDocument().models[0]).toMatchObject({ id: 'renamed', context_length: 8192 });
  });

  it('shows one field per value, each a simple string, under the setting', () => {
    renderDocument();

    const fields = within(screen.getByRole('group', { name: 'MODEL_CATALOG' }));
    expect(fields.getAllByRole('textbox')).toHaveLength(6);
    expect(field('models[0].id')).toHaveValue('example-chat');
    expect(field('models[0].context_length')).toHaveValue('4096');
    expect(field('models[0].supports_tools')).toHaveValue('false');
    expect(field('models[0].supported_params')).toHaveValue('temperature, top_p');
    expect(field('models[0].route[0].base_url')).toHaveValue('${EXAMPLE_UPSTREAM_BASE_URL}');
    // What to type, where it is not plain text.
    expect(field('models[0].context_length')).toHaveAccessibleDescription('number');
    expect(field('models[0].supports_tools')).toHaveAccessibleDescription('true or false');
    expect(field('models[0].supported_params')).toHaveAccessibleDescription('comma-separated');
    expect(screen.getByRole('button', { name: 'Fields' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('textbox', { name: 'MODEL_CATALOG' })).not.toBeInTheDocument();
  });

  it('saves the whole document, each value as its type, and an edit put back is no edit', () => {
    renderDocument();

    type('models[0].context_length', '8192');
    type('models[0].supports_tools', 'true');
    type('models[0].supported_params', 'temperature, top_p, stop');
    expect(draftDocument()).toEqual({
      models: [
        {
          id: 'example-chat',
          context_length: 8192,
          supports_tools: true,
          supported_params: ['temperature', 'top_p', 'stop'],
          route: [{ base_url: '${EXAMPLE_UPSTREAM_BASE_URL}', weight: 1 }],
        },
      ],
    });
    // Saved indented, which the JSON view then shows.
    expect(latest).toMatchObject({ kind: 'value', value: expect.stringContaining('\n  "models"') });
    expect(buildPatch([documentEntry()], { MODEL_CATALOG: latest! }).errors).toEqual({});

    type('models[0].context_length', '4096');
    type('models[0].supports_tools', 'false');
    type('models[0].supported_params', 'temperature,top_p');
    expect(latest).toBeUndefined();
  });

  it('keeps what is typed, and holds the save back while a value is not of its type', () => {
    renderDocument();

    type('models[0].context_length', '8k');
    expect(field('models[0].context_length')).toHaveValue('8k');
    expect(field('models[0].context_length')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('Enter a number.')).toBeInTheDocument();
    expect(draftError(documentEntry(), latest)).toBe('models[0].context_length: Enter a number.');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'models[0].context_length: Enter a number.',
    );
    expect(buildPatch([documentEntry()], { MODEL_CATALOG: latest! })).toEqual({
      values: {},
      errors: { MODEL_CATALOG: 'models[0].context_length: Enter a number.' },
    });

    // Another field still goes into the document meanwhile.
    type('models[0].id', 'example-chat-2');
    expect(draftDocument().models[0]).toMatchObject({ id: 'example-chat-2', context_length: 4096 });

    type('models[0].context_length', '8000');
    expect(screen.queryByText('Enter a number.')).not.toBeInTheDocument();
    expect(draftError(documentEntry(), latest)).toBeNull();
    expect(draftDocument().models[0]).toMatchObject({ id: 'example-chat-2', context_length: 8000 });
  });

  it('puts the saved values back on Discard', () => {
    renderDocument();
    type('models[0].id', 'renamed');
    type('models[0].context_length', 'x');

    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));

    expect(field('models[0].id')).toHaveValue('example-chat');
    expect(field('models[0].context_length')).toHaveValue('4096');
    expect(screen.queryByText('Enter a number.')).not.toBeInTheDocument();
  });

  it('adds and removes entries in the JSON view, and shows them as fields after', () => {
    renderDocument();
    type('models[0].id', 'renamed');

    fireEvent.click(screen.getByRole('button', { name: 'JSON' }));
    const json = screen.getByRole('textbox', { name: 'MODEL_CATALOG' });
    // The fields' edit, as the document the JSON view edits.
    expect(JSON.parse((json as HTMLTextAreaElement).value).models[0].id).toBe('renamed');
    fireEvent.change(json, {
      target: { value: JSON.stringify({ models: [{ id: 'renamed', aliases: ['chat'] }] }) },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Fields' }));
    expect(field('models[0].id')).toHaveValue('renamed');
    expect(field('models[0].aliases')).toHaveValue('chat');
    expect(screen.queryByRole('textbox', { name: 'models[0].context_length' })).toBeNull();
  });

  it('leaves a value that does not convert behind in the fields when the JSON view opens', () => {
    renderDocument();
    type('models[0].context_length', '8k');
    expect(latest).toMatchObject({ error: expect.any(String) });

    fireEvent.click(screen.getByRole('button', { name: 'JSON' }));

    expect(latest).toBeUndefined();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows no fields for text that is not a JSON object', () => {
    renderDocument();
    fireEvent.click(screen.getByRole('button', { name: 'JSON' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'MODEL_CATALOG' }), {
      target: { value: '{"models": [' },
    });

    expect(screen.getByRole('button', { name: 'Fields' })).toBeDisabled();
    expect(screen.getByText(/not a JSON object, so it has no fields to show/)).toBeInTheDocument();
  });

  it('opens an empty document on its JSON, where its first values go', () => {
    renderDocument(documentEntry({ value: '', is_set: false, source: 'default' }));

    expect(screen.getByRole('button', { name: 'JSON' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('textbox', { name: 'MODEL_CATALOG' })).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Fields' }));
    expect(screen.getByText('No values yet. Add them in JSON.')).toBeInTheDocument();
  });

  it('marks the field the reason for a refused save names', () => {
    renderDocument(
      documentEntry(),
      'MODEL_CATALOG: /models/0/route/0/base_url must start with http:// or https://',
    );

    expect(field('models[0].route[0].base_url')).toHaveAttribute('aria-invalid', 'true');
    expect(field('models[0].id')).not.toHaveAttribute('aria-invalid');
  });

  it('narrows a long document to the fields a filter matches', () => {
    const settings = Object.fromEntries(
      Array.from({ length: 14 }, (_, i) => [`setting_${i}`, `value ${i}`]),
    );
    renderDocument(documentEntry({ value: JSON.stringify(settings) }));

    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter MODEL_CATALOG fields' }), {
      target: { value: 'value 13' },
    });

    const shown = within(screen.getByRole('group', { name: 'MODEL_CATALOG' })).getAllByRole(
      'textbox',
    );
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveValue('value 13');
  });

  it('keeps a document with an integer beyond 2^53 in JSON, which fields would round', () => {
    const big = '{"models": [{"id": "example-chat", "seed": 9007199254740993}]}';
    renderDocument(documentEntry({ value: big }));

    expect(screen.getByRole('button', { name: 'JSON' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Fields' })).toBeDisabled();
    expect(screen.getByText(/a number too large to edit as fields/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'MODEL_CATALOG' })).toHaveValue(big);
  });

  it('shows an empty list without a field to type into, and saves a never-set value as text', () => {
    renderDocument(
      documentEntry({ value: JSON.stringify({ local_deployment: [], release: null, name: 'x' }) }),
    );

    const fields = within(screen.getByRole('group', { name: 'MODEL_CATALOG' }));
    expect(fields.getByText('An empty list. Add its items in JSON.')).toBeInTheDocument();
    expect(fields.getAllByRole('textbox')).toHaveLength(2);
    expect(field('release')).toHaveAttribute('placeholder', 'null');

    type('release', '2024');
    expect(draftDocument()).toEqual({ local_deployment: [], release: '2024', name: 'x' });
  });

  it("labels the fields and the JSON view with the setting's name, and points at neither", () => {
    renderDocument();
    const label = screen.getByText('MODEL_CATALOG', { selector: 'label' });
    expect(label).not.toHaveAttribute('for');
    expect(screen.getByRole('group', { name: 'MODEL_CATALOG' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'JSON' }));
    expect(screen.getByRole('textbox', { name: 'MODEL_CATALOG' })).toHaveAttribute(
      'aria-labelledby',
      label.id,
    );
  });

  it('is plain text on a backend that does not say it is a document', () => {
    renderDocument(documentEntry({ document: undefined }));

    expect(screen.getByRole('textbox', { name: 'MODEL_CATALOG' })).toHaveValue(CATALOG);
    expect(screen.queryByRole('button', { name: 'Fields' })).not.toBeInTheDocument();
  });
});
