// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import { ConfigEntryField } from './ConfigEntryField';
import { buildPatch, type ConfigDraft } from './configDrafts';

function policyEntry(value: string | null, overrides: Partial<ConfigEntry> = {}): ConfigEntry {
  return {
    key: 'ROUTING_POLICY',
    category: 'application',
    description: 'Optional routing policy as JSON. Omit to use the default Fixed router.',
    type: 'text',
    secret: false,
    required: false,
    missing: false,
    is_set: value !== null,
    value,
    default: null,
    source: value !== null ? 'database' : 'default',
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

function renderPolicy(entry: ConfigEntry, serverError?: string) {
  latest = undefined;
  return render(<Harness entry={entry} serverError={serverError} />);
}

const saved = () => JSON.parse((latest as { value: string }).value);

afterEach(cleanup);

describe('RoutingPolicyEditor', () => {
  it('shows an unset policy as a card, saying what the defaults do', () => {
    renderPolicy(policyEntry(null));
    const card = within(screen.getByRole('group', { name: 'ROUTING_POLICY' }));
    expect(card.getByRole('list', { name: 'What this policy does' })).toHaveTextContent(
      'Models that name no router use Fixed.',
    );
    expect(card.getByRole('combobox', { name: /Router/ })).toHaveValue('');
    expect(card.getByRole('textbox', { name: /Local share/ })).toHaveAttribute(
      'placeholder',
      '0.5',
    );
    expect(card.getAllByText('None listed.')).toHaveLength(2);
    expect(latest).toBeUndefined();
  });

  it('writes the chosen router into the policy, and back out on Default', () => {
    renderPolicy(policyEntry(null));
    const router = screen.getByRole('combobox', { name: /Router/ });
    fireEvent.change(router, { target: { value: 'routewise' } });
    expect(saved()).toEqual({ default_router: 'routewise' });
    expect(screen.getByText('Models that name no router use RouteWise.')).toBeInTheDocument();
    fireEvent.change(router, { target: { value: '' } });
    // Back to what is saved: nothing to save.
    expect(latest).toBeUndefined();
  });

  it('adds a deployment, holding the save until it is complete', () => {
    renderPolicy(policyEntry('{"health_check": 10}'));
    fireEvent.click(screen.getByRole('button', { name: 'Add local deployment' }));
    expect(screen.getByText('Enter the endpoint URL.')).toBeInTheDocument();
    expect(
      buildPatch([policyEntry('{"health_check": 10}')], { ROUTING_POLICY: latest! }).errors,
    ).toHaveProperty('ROUTING_POLICY');

    fireEvent.change(screen.getByRole('textbox', { name: 'Endpoint of local deployment 1' }), {
      target: { value: 'http://10.0.0.5:8000' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: 'Models of local deployment 1' }), {
      target: { value: 'glm-4.6, qwen3' },
    });
    expect((latest as { error?: string }).error).toBeUndefined();
    expect(saved()).toEqual({
      health_check: 10,
      local_deployment: [{ endpoint: 'http://10.0.0.5:8000', models: ['glm-4.6', 'qwen3'] }],
    });
    expect(screen.getByText(/checked every 10s and reported on GET \/routing/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Remove local deployment 1' }));
    expect(latest).toBeUndefined();
  });

  it('says beside a number what is wrong with it', () => {
    renderPolicy(policyEntry(null));
    fireEvent.change(screen.getByRole('textbox', { name: /Local share/ }), {
      target: { value: '2' },
    });
    expect(screen.getByText('Enter a share from 0 to 1.')).toBeInTheDocument();
    expect((latest as { error?: string }).error).toContain('routing_parameter/local_fraction');
  });

  it('keeps keys it does not edit', () => {
    const value = JSON.stringify({
      custom: 1,
      remote_deployment: [{ endpoint: 'https://a', models: ['m'], note: 'x' }],
    });
    renderPolicy(policyEntry(value));
    expect(screen.getByText(/1 other key is kept/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: /Check timeout/ }), {
      target: { value: '5' },
    });
    expect(saved()).toEqual({ ...JSON.parse(value), timeout: 5 });
  });

  it('marks the field a refused save names', () => {
    renderPolicy(policyEntry('{"timeout": 3}'), '/timeout value must be non-negative');
    expect(screen.getByRole('textbox', { name: /Check timeout/ })).toHaveAttribute(
      'aria-invalid',
      'true',
    );
  });

  it('opens a policy the card cannot show as JSON', () => {
    renderPolicy(policyEntry('{"timeout": "2"}'));
    expect(screen.getByRole('button', { name: 'Policy' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'ROUTING_POLICY' })).toHaveValue('{"timeout": "2"}');
  });

  it('starts again from the saved policy on Discard', () => {
    renderPolicy(policyEntry('{"timeout": 3}'));
    const timeout = screen.getByRole('textbox', { name: /Check timeout/ });
    fireEvent.change(timeout, { target: { value: '7' } });
    expect(timeout).toHaveValue('7');
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(screen.getByRole('textbox', { name: /Check timeout/ })).toHaveValue('3');
  });
});
