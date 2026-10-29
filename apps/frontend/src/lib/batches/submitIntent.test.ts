// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchConsoleError, type BatchDetail, type ConsoleSubmission } from '@/lib/api/batches';

import {
  DEFAULT_REPLAY_SECONDS,
  indexedDbIntentStore,
  localIntentStore,
  newIntent,
  REPLAY_MARGIN_MS,
  settleIntent,
  withIntentStore,
  type SubmitIntent,
} from './submitIntent';
import { fakeIndexedDb } from './__tests__/fakeIndexedDb';

const BODY: ConsoleSubmission = {
  source_file_id: 'file-1',
  source_sha256: 'a'.repeat(64),
  endpoint: '/v1/chat/completions',
  overrides: { temperature: 0 },
  confirmed_model: { requested: 'm', canonical: 'm' },
  credential: { key_id: '1', revision: 'r' },
  name: 'eval',
  description: '',
  copied_from: null,
};

function batch(id: string, userId = 'alice'): BatchDetail {
  return { id, user_id: userId } as BatchDetail;
}

describe('the intent store', () => {
  beforeEach(() => window.localStorage.clear());

  it('keeps one intent per account and clears it only by its own key', async () => {
    const first = newIntent('alice', BODY, 1);
    const second = newIntent('alice', BODY, 2);
    expect(await localIntentStore.createIfAbsent(first)).toEqual(first);
    // A second tab never replaces a pending intent: it gets the first one back.
    expect(await localIntentStore.createIfAbsent(second)).toEqual(first);
    await localIntentStore.clear('alice', second.key);
    expect(await localIntentStore.get('alice')).toEqual(first);
    await localIntentStore.clear('alice', first.key);
    expect(await localIntentStore.get('alice')).toBeNull();
  });

  it('never shows one account the intent of another', async () => {
    await localIntentStore.createIfAbsent(newIntent('alice', BODY));
    expect(await localIntentStore.get('bob')).toBeNull();
    // A record copied under another account's slot is not trusted either.
    window.localStorage.setItem(
      'batch-submit:bob',
      window.localStorage.getItem('batch-submit:alice') ?? '',
    );
    expect(await localIntentStore.get('bob')).toBeNull();
    expect(await localIntentStore.get('alice')).not.toBeNull();
  });

  it('creates atomically in IndexedDB when two tabs race without Web Locks', async () => {
    const store = indexedDbIntentStore(fakeIndexedDb());
    const a = newIntent('alice', BODY, 1);
    const b = newIntent('alice', BODY, 2);
    const [won, lost] = await Promise.all([store.createIfAbsent(a), store.createIfAbsent(b)]);
    expect(won.key).toBe(a.key);
    expect(lost.key).toBe(a.key);
    expect((await store.get('alice'))?.key).toBe(a.key);
    await store.clear('alice', b.key);
    expect((await store.get('alice'))?.key).toBe(a.key);
    await store.clear('alice', a.key);
    expect(await store.get('alice')).toBeNull();
  });

  describe('without coordination', () => {
    const locks = Object.getOwnPropertyDescriptor(navigator, 'locks');
    const idb = (globalThis as { indexedDB?: unknown }).indexedDB;
    afterEach(() => {
      if (locks) Object.defineProperty(navigator, 'locks', locks);
      (globalThis as { indexedDB?: unknown }).indexedDB = idb;
    });

    it('refuses to make an intent rather than submit unprotected', async () => {
      Object.defineProperty(navigator, 'locks', { value: undefined, configurable: true });
      delete (globalThis as { indexedDB?: unknown }).indexedDB;
      await expect(withIntentStore('alice', async () => 1)).rejects.toThrow(/coordinate/);
    });
  });

  it('serializes an account across tabs with a Web Lock', async () => {
    const held: string[] = [];
    const request = vi.fn(async (name: string, fn: () => Promise<unknown>) => {
      held.push(name);
      return fn();
    });
    Object.defineProperty(navigator, 'locks', { value: { request }, configurable: true });
    await withIntentStore('alice', async (store) => store.get('alice'));
    expect(held).toEqual(['batch-submit:alice']);
  });
});

describe('settling an intent', () => {
  const day = DEFAULT_REPLAY_SECONDS * 1000;

  it('finds what a lost response created and never sends it again', async () => {
    const intent = newIntent('alice', BODY, 0);
    const create = vi.fn();
    const outcome = await settleIntent(intent, {
      lookup: async () => ({ state: 'created', batch: batch('batch_1') }),
      create,
      now: () => 5 * day,
    });
    expect(outcome).toEqual({ kind: 'created', batch: batch('batch_1') });
    expect(create).not.toHaveBeenCalled();
  });

  it('sends the same key and body again while the gateway must still recognise it', async () => {
    const intent = newIntent('alice', BODY, 0);
    const create = vi.fn(async () => batch('batch_2'));
    const outcome = await settleIntent(intent, {
      lookup: async () => ({ state: 'unknown', batch: null }),
      create,
      now: () => day - REPLAY_MARGIN_MS - 1,
    });
    expect(outcome.kind).toBe('created');
    expect(create).toHaveBeenCalledWith(BODY, intent.key);
  });

  it('stops for good once the outcome can no longer be established', async () => {
    // Committed, response lost; the batch finished, its record expired and was
    // swept, the batch was purged; the browser comes back two days later.
    const intent = newIntent('alice', BODY, 0);
    const create = vi.fn();
    const outcome = await settleIntent(intent, {
      lookup: async () => ({ state: 'unknown', batch: null }),
      create,
      now: () => 2 * day,
    });
    expect(outcome).toEqual({ kind: 'stale' });
    expect(create).not.toHaveBeenCalled();
  });

  it('honours the gateway window it was given', async () => {
    const intent = newIntent('alice', BODY, 0);
    const create = vi.fn(async () => batch('b'));
    const deps = {
      lookup: async () => ({ state: 'unknown' as const, batch: null }),
      create,
      replaySeconds: 3 * 3600,
    };
    expect((await settleIntent(intent, { ...deps, now: () => 3 * 3600 * 1000 })).kind).toBe(
      'stale',
    );
    expect((await settleIntent(intent, { ...deps, now: () => 3600 * 1000 })).kind).toBe('created');
  });

  it('reports a deleted batch and does not recreate it', async () => {
    const intent = newIntent('alice', BODY, 0);
    expect(
      await settleIntent(intent, {
        lookup: async () => ({ state: 'deleted', batch: null }),
        create: vi.fn(),
      }),
    ).toEqual({ kind: 'deleted' });
    const gone = await settleIntent(intent, {
      lookup: async () => ({ state: 'unknown', batch: null }),
      create: async () => {
        throw new BatchConsoleError(410, null, 'gone');
      },
      now: () => 1,
    });
    expect(gone).toEqual({ kind: 'deleted' });
  });

  it('keeps an intent whose answer is not known yet', async () => {
    const intent = newIntent('alice', BODY, 0);
    for (const error of [
      new TypeError('network'),
      new BatchConsoleError(429, 'scan_busy', 'busy'),
      new BatchConsoleError(503, 'scan_timeout', 'slow'),
      new BatchConsoleError(502, null, 'bad gateway'),
    ]) {
      const outcome = await settleIntent(
        intent,
        {
          lookup: async () => ({ state: 'unknown', batch: null }),
          create: async () => {
            throw error;
          },
          now: () => 1,
        },
        { lookupFirst: false },
      );
      expect(outcome.kind).toBe('pending');
    }
    // A lookup that fails is not an answer either.
    const outcome = await settleIntent(intent, {
      lookup: async () => {
        throw new TypeError('offline');
      },
      create: vi.fn(),
    });
    expect(outcome.kind).toBe('pending');
  });

  it('ends on a definite refusal', async () => {
    const intent = newIntent('alice', BODY, 0);
    for (const [status, code] of [
      [409, 'credential_changed'],
      [409, 'model_resolution_changed'],
      [409, 'source_unavailable'],
      [409, null],
      [422, 'invalid_file'],
      [429, 'quota_exceeded'],
      [403, 'role_required'],
    ] as const) {
      const outcome = await settleIntent(
        intent,
        {
          lookup: vi.fn(),
          create: async () => {
            throw new BatchConsoleError(status, code, 'no');
          },
          now: () => 1,
        },
        { lookupFirst: false },
      );
      expect(outcome.kind).toBe('refused');
    }
  });

  it('accepts no answer naming another account', async () => {
    const intent: SubmitIntent = newIntent('alice', BODY, 0);
    const outcome = await settleIntent(intent, {
      lookup: async () => ({ state: 'created', batch: batch('batch_x', 'bob') }),
      create: vi.fn(),
    });
    expect(outcome.kind).toBe('refused');
  });
});

describe('replacing a stale intent', () => {
  beforeEach(() => window.localStorage.clear());

  for (const [name, make] of [
    ['localStorage', () => localIntentStore],
    ['IndexedDB', () => indexedDbIntentStore(fakeIndexedDb())],
  ] as const) {
    it(`replaces only the intent the user saw (${name})`, async () => {
      const store = make();
      const shown = newIntent('alice', BODY, 0);
      await store.createIfAbsent(shown);
      // Another tab already replaced it.
      const other = newIntent('alice', BODY, 1);
      expect((await store.replaceIf('alice', shown.key, other))?.key).toBe(other.key);
      const late = newIntent('alice', BODY, 2);
      expect((await store.replaceIf('alice', shown.key, late))?.key).toBe(other.key);
      expect((await store.get('alice'))?.key).toBe(other.key);
      // Nor does a late discard of the shown key remove the newer one.
      await store.clear('alice', shown.key);
      expect((await store.get('alice'))?.key).toBe(other.key);
      // Nothing pending: nothing is replaced.
      await store.clear('alice', other.key);
      expect(await store.replaceIf('alice', other.key, late)).toBeNull();
      expect(await store.get('alice')).toBeNull();
    });
  }
});
