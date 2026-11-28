// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import { ConfigEntryField } from './ConfigEntryField';
import { buildPatch, type ConfigDraft } from './configDrafts';

const CATALOG = {
  models: [
    {
      id: 'chat-a',
      name: 'Chat A',
      provider: 'vllm',
      context_length: 32768,
      supports_tools: true,
      pricing: { input: '0.5' },
      route: [
        { kind: 'vllm', base_url: '${A_URL}', weight: 3, null_cache_details_means_miss: true },
        { kind: 'openrouter', base_url: 'https://openrouter.ai/api/v1', weight: 1 },
      ],
    },
    { id: 'embed-b', provider: 'openai_compat', base_url: '${B_URL}', model_type: 'embedding' },
  ],
};

function catalogEntry(value: unknown = CATALOG): ConfigEntry {
  return {
    key: 'MODEL_CATALOG',
    category: 'models',
    description: 'Model catalog as JSON.',
    type: 'text',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: typeof value === 'string' ? value : JSON.stringify(value),
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
  };
}

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

function renderCatalog(entry = catalogEntry(), serverError?: string) {
  latest = undefined;
  return render(<Harness entry={entry} serverError={serverError} />);
}

const card = (id: string) => {
  const element = document.querySelector(`[data-model-card="${id}"]`);
  if (!element) throw new Error(`no card for ${id}`);
  return within(element as HTMLElement);
};
const draftDocument = () => JSON.parse(String(latest && latest.kind === 'value' && latest.value));
const type = (scope: ReturnType<typeof card>, label: string, value: string) =>
  fireEvent.change(scope.getByLabelText(label), { target: { value } });

afterEach(() => cleanup());

describe('the model catalog editor', () => {
  it('shows each model as a card with a summary, open when there are only a few', () => {
    renderCatalog();

    expect(screen.getByRole('button', { name: 'Models' })).toHaveAttribute('aria-pressed', 'true');
    const a = card('chat-a');
    expect(a.getByText('2 routes')).toBeInTheDocument();
    expect(a.getByText('32K ctx')).toBeInTheDocument();
    expect(a.getByText('tools')).toBeInTheDocument();
    expect(a.getByLabelText(/Model id/)).toHaveValue('chat-a');
    expect(a.getByText('75% of traffic')).toBeInTheDocument();
    // A route key the rows do not edit is kept, and said to be.
    expect(a.getByText('null_cache_details_means_miss')).toBeInTheDocument();
    // A shorthand model shows its one endpoint.
    const b = card('embed-b');
    expect(b.getByText('1 endpoint')).toBeInTheDocument();
    expect(b.getByLabelText('Base URL')).toHaveValue('${B_URL}');
  });

  it('writes edits into the catalog and keeps every key it does not show', () => {
    renderCatalog();
    const a = card('chat-a');

    type(a, 'Context length', '65536');
    fireEvent.click(a.getByLabelText('Supports structured output (JSON schema)'));
    type(a, 'Aliases', 'a, alpha');
    const doc = draftDocument();
    expect(doc.models[0]).toMatchObject({
      context_length: 65536,
      supports_structured_output: true,
      aliases: ['a', 'alpha'],
      pricing: { input: '0.5' },
    });
    expect(doc.models[0].route[0].null_cache_details_means_miss).toBe(true);
    expect(doc.models[1]).toEqual(CATALOG.models[1]);
    expect(buildPatch([catalogEntry()], { MODEL_CATALOG: latest! }).errors).toEqual({});
  });

  it('keeps a list as typed while it is being extended', () => {
    renderCatalog();
    const a = card('chat-a');

    type(a, 'Aliases', 'a, ');
    expect(a.getByLabelText('Aliases')).toHaveValue('a, ');
    expect(draftDocument().models[0].aliases).toEqual(['a']);
  });

  it('holds the save back for a number that is not one, and says why', () => {
    renderCatalog();
    const a = card('chat-a');

    type(a, 'Context length', '32k');
    expect(a.getByText('Enter a whole number.')).toBeInTheDocument();
    expect(latest).toMatchObject({
      kind: 'value',
      error: expect.stringContaining('Context length'),
    });
    type(a, 'Context length', '32768');
    // Back where it started: no edit at all.
    expect(latest).toBeUndefined();
  });

  it('adds, duplicates and removes models, refusing a missing or repeated id', () => {
    renderCatalog();

    fireEvent.click(screen.getByRole('button', { name: '+ Add model' }));
    expect(draftDocument().models.map((m: { id: string }) => m.id)).toEqual([
      'chat-a',
      'embed-b',
      'new-model',
    ]);
    const added = card('new-model');
    type(added, 'Model id *', 'chat-a');
    expect(latest).toMatchObject({ error: expect.stringContaining('already has the id') });
    type(added, 'Model id *', 'chat-c');
    expect(latest).not.toHaveProperty('error');

    fireEvent.click(card('chat-a').getByRole('button', { name: 'Duplicate' }));
    expect(draftDocument().models[1].id).toBe('chat-a-copy');

    fireEvent.click(card('chat-a-copy').getByRole('button', { name: 'Remove model' }));
    fireEvent.click(card('chat-a-copy').getByRole('button', { name: 'Remove' }));
    expect(draftDocument().models.map((m: { id: string }) => m.id)).toEqual([
      'chat-a',
      'embed-b',
      'chat-c',
    ]);
  });

  it('adds and removes routes, and turns a shorthand into routes to add a second', () => {
    renderCatalog();

    fireEvent.click(card('embed-b').getByRole('button', { name: '+ Add a second route' }));
    const b = draftDocument().models[1];
    expect(b.base_url).toBeUndefined();
    expect(b.route).toEqual([
      { weight: 1, base_url: '${B_URL}' },
      expect.objectContaining({ weight: 1 }),
    ]);

    fireEvent.click(card('chat-a').getByRole('button', { name: 'Remove route 2' }));
    expect(draftDocument().models[0].route).toHaveLength(1);
  });

  it('edits the keys the form does not cover as the model’s own JSON', () => {
    renderCatalog();
    const a = card('chat-a');

    fireEvent.click(a.getByRole('button', { name: /Advanced/ }));
    const advanced = a.getByLabelText('Other model settings as JSON');
    expect(advanced).toHaveValue(JSON.stringify({ pricing: { input: '0.5' } }, null, 2));
    fireEvent.change(advanced, { target: { value: '{"pricing": ' } });
    expect(latest).toMatchObject({ error: expect.stringContaining('Advanced JSON') });
    fireEvent.change(advanced, { target: { value: '{"processor": "default"}' } });
    const model = draftDocument().models[0];
    expect(model.pricing).toBeUndefined();
    expect(model.processor).toBe('default');
    expect(model.id).toBe('chat-a');
  });

  it('counts the saved catalog put back as no edit, even one with a problem already', () => {
    renderCatalog(catalogEntry({ models: [{ id: 'bare', context_length: 4096 }] }));
    const bare = card('bare');

    type(bare, 'Context length', '8192');
    expect(latest).toMatchObject({ error: expect.stringContaining('Name a provider') });
    type(bare, 'Context length', '4096');
    expect(latest).toBeUndefined();
  });

  it('starts again from the saved catalog on Discard', () => {
    renderCatalog();
    type(card('chat-a'), 'Context length', 'oops');
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));

    expect(latest).toBeUndefined();
    expect(card('chat-a').getByLabelText('Context length')).toHaveValue('32768');
    expect(card('chat-a').queryByText('Enter a whole number.')).not.toBeInTheDocument();
  });

  it('opens the model a refused save names', () => {
    const many = { models: ['m1', 'm2', 'm3', 'm4'].map((id) => ({ id, provider: 'vllm' })) };
    renderCatalog(catalogEntry(many), "weight must be a number, 0 or more (model 'm3', route 1)");

    expect(card('m3').getByLabelText(/Model id/)).toBeInTheDocument();
    expect(card('m1').queryByLabelText(/Model id/)).not.toBeInTheDocument();
  });

  it('falls back to JSON for a catalog it cannot keep exactly', () => {
    renderCatalog(catalogEntry('{"models": {"not": "a list"}}'));

    expect(screen.getByRole('button', { name: 'Models' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'MODEL_CATALOG' })).toBeInTheDocument();
  });
});
