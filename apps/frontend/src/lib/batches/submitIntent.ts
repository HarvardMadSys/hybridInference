/**
 * One console submission, kept until its outcome is known.
 *
 * Clicking "Submit" makes a *submission intent*: an `Idempotency-Key`
 * (`console-<uuid>`), the exact body, and when it was made. It is written
 * before the first request and kept per account until the outcome is
 * certain, so a timeout, a reload or another tab recovers the same intent with
 * the same key and body instead of making a new one -- the gateway answers a
 * repeated key with the batch it already created.
 *
 * Coordination across tabs: every read, write and request of an account's
 * intent runs under the Web Lock `batch-submit:<user_id>`, with the record in
 * `localStorage`. Without Web Locks the record lives in IndexedDB instead,
 * created and cleared in read-write transactions (so two tabs can never both
 * create one), and IndexedDB is then its only home. Without either, no new
 * intent can be made safely, and the page says so rather than submit without
 * protection.
 *
 * Recovery never trusts a key forever. The gateway is certain to recognise a
 * key for `submission_replay_seconds` (a day) after the batch it created ends;
 * a console batch also answers it for as long as the batch is still stored.
 * So recovery first asks, read only, what the key created
 * (`GET /user/batch-submissions/{key}`). Only an intent younger than that
 * guaranteed window is sent again with its key. An older one whose outcome
 * cannot be found is *stale*: it is never sent again automatically, and only
 * a deliberate new submission -- a new key, which can create (and bill) a
 * second batch -- replaces it.
 *
 * Accounts never mix: records are per user id, a page reads only its signed-in
 * account's, and a response naming another account is not accepted.
 */

import type { BatchDetail, ConsoleSubmission, SubmissionLookup } from '@/lib/api/batches';
import { BatchConsoleError } from '@/lib/api/batches';

export interface SubmitIntent {
  version: 1;
  userId: string;
  key: string;
  body: ConsoleSubmission;
  /** When the intent was made (ms since the epoch, this browser's clock). */
  createdAt: number;
}

/** Atomic operations on one account's intent. */
export interface IntentStore {
  get(userId: string): Promise<SubmitIntent | null>;
  /** Store `intent` unless the account already has one; return the one that is now stored. */
  createIfAbsent(intent: SubmitIntent): Promise<SubmitIntent>;
  /** Remove the account's intent if it is still the one with `key`. */
  clear(userId: string, key: string): Promise<void>;
  /**
   * Replace the account's intent with `next` only if it is still the one with
   * `expectedKey`; return the intent stored afterwards (`next`, another tab's,
   * or null when there is none).
   */
  replaceIf(userId: string, expectedKey: string, next: SubmitIntent): Promise<SubmitIntent | null>;
}

export class IntentUnsupportedError extends Error {
  constructor() {
    super('This browser cannot coordinate submissions safely across tabs.');
    this.name = 'IntentUnsupportedError';
  }
}

const PREFIX = 'batch-submit:';
/** Kept below the gateway's guaranteed window, for clocks and slow requests. */
export const REPLAY_MARGIN_MS = 60 * 60 * 1000;
export const DEFAULT_REPLAY_SECONDS = 24 * 3600;

function parse(raw: string | null, userId: string): SubmitIntent | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as SubmitIntent;
    if (value?.version !== 1 || value.userId !== userId || typeof value.key !== 'string') {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

/** `localStorage` under the account's Web Lock (the caller holds it). */
export const localIntentStore: IntentStore = {
  async get(userId) {
    return parse(window.localStorage.getItem(PREFIX + userId), userId);
  },
  async createIfAbsent(intent) {
    const existing = await this.get(intent.userId);
    if (existing) return existing;
    window.localStorage.setItem(PREFIX + intent.userId, JSON.stringify(intent));
    return intent;
  },
  async clear(userId, key) {
    const existing = await this.get(userId);
    if (existing?.key === key) window.localStorage.removeItem(PREFIX + userId);
  },
  async replaceIf(userId, expectedKey, next) {
    const existing = await this.get(userId);
    if (existing?.key !== expectedKey) return existing;
    window.localStorage.setItem(PREFIX + userId, JSON.stringify(next));
    return next;
  },
};

const DB_NAME = 'hybridinference-batch-submit';
const DB_STORE = 'intents';

function openDb(factory: IDBFactory): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(DB_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function inTransaction<T>(
  factory: IDBFactory,
  run: (store: IDBObjectStore, done: (value: T) => void) => void,
): Promise<T> {
  return openDb(factory).then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        let result: T;
        run(tx.objectStore(DB_STORE), (value) => {
          result = value;
        });
        tx.oncomplete = () => {
          db.close();
          resolve(result);
        };
        tx.onerror = () => {
          db.close();
          reject(tx.error);
        };
        tx.onabort = () => {
          db.close();
          reject(tx.error);
        };
      }),
  );
}

/** IndexedDB, each operation one read-write transaction: atomic without a lock. */
export function indexedDbIntentStore(factory: IDBFactory): IntentStore {
  return {
    get(userId) {
      return inTransaction<SubmitIntent | null>(factory, (store, done) => {
        const read = store.get(PREFIX + userId);
        read.onsuccess = () => done(parse(read.result ?? null, userId));
      });
    },
    createIfAbsent(intent) {
      return inTransaction<SubmitIntent>(factory, (store, done) => {
        const read = store.get(PREFIX + intent.userId);
        read.onsuccess = () => {
          const existing = parse(read.result ?? null, intent.userId);
          if (existing) {
            done(existing);
            return;
          }
          store.put(JSON.stringify(intent), PREFIX + intent.userId);
          done(intent);
        };
      });
    },
    clear(userId, key) {
      return inTransaction<void>(factory, (store, done) => {
        const read = store.get(PREFIX + userId);
        read.onsuccess = () => {
          if (parse(read.result ?? null, userId)?.key === key) store.delete(PREFIX + userId);
          done(undefined);
        };
      });
    },
    replaceIf(userId, expectedKey, next) {
      return inTransaction<SubmitIntent | null>(factory, (store, done) => {
        const read = store.get(PREFIX + userId);
        read.onsuccess = () => {
          const existing = parse(read.result ?? null, userId);
          if (existing?.key !== expectedKey) {
            done(existing);
            return;
          }
          store.put(JSON.stringify(next), PREFIX + userId);
          done(next);
        };
      });
    },
  };
}

/**
 * Run `fn` with the account's intent store, coordinated across tabs.
 *
 * @throws IntentUnsupportedError when neither Web Locks nor IndexedDB exists.
 */
export async function withIntentStore<T>(
  userId: string,
  fn: (store: IntentStore) => Promise<T>,
): Promise<T> {
  if (typeof navigator !== 'undefined' && navigator.locks) {
    return navigator.locks.request(PREFIX + userId, () => fn(localIntentStore));
  }
  if (typeof indexedDB !== 'undefined') return fn(indexedDbIntentStore(indexedDB));
  throw new IntentUnsupportedError();
}

/** A new intent: a new key for one deliberate submission. */
export function newIntent(userId: string, body: ConsoleSubmission, now = Date.now()): SubmitIntent {
  const id =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${now.toString(36)}-${Math.random().toString(36).slice(2)}`;
  return { version: 1, userId, key: `console-${id}`, body, createdAt: now };
}

/** How a settled (or not yet settled) submission ended. */
export type IntentOutcome =
  /** The batch exists; the intent is done. */
  | { kind: 'created'; batch: BatchDetail }
  /** It created a batch that has since been deleted; the intent is done. */
  | { kind: 'deleted' }
  /** Refused for good (nothing created); the intent is done. */
  | { kind: 'refused'; error: BatchConsoleError }
  /** Not known yet (network, timeout, busy); kept, to be tried again with the same key. */
  | { kind: 'pending'; error: unknown }
  /**
   * Too old to send again safely and nothing found under its key; kept until
   * the user decides. Never sent again automatically.
   */
  | { kind: 'stale' };

export interface SettleDeps {
  lookup: (key: string) => Promise<SubmissionLookup>;
  create: (body: ConsoleSubmission, key: string) => Promise<BatchDetail>;
  now?: () => number;
  /** The gateway's guaranteed replay window, in seconds. */
  replaySeconds?: number;
}

/** Whether a refused submission's answer is final (nothing was or will be created by it). */
function isFinal(error: unknown): error is BatchConsoleError {
  if (!(error instanceof BatchConsoleError)) return false;
  if (error.status === 429) return error.code !== 'scan_busy';
  if (error.status === 503 || error.status >= 500) return false;
  return error.status >= 400;
}

/**
 * Settle one intent: find what its key created, or send it again while that is safe.
 *
 * Never makes a new key. Call under {@link withIntentStore}, and clear the
 * intent for every outcome but `pending` and `stale`.
 */
export async function settleIntent(
  intent: SubmitIntent,
  deps: SettleDeps,
  { lookupFirst = true }: { lookupFirst?: boolean } = {},
): Promise<IntentOutcome> {
  const accept = (batch: BatchDetail): IntentOutcome =>
    batch.user_id && batch.user_id !== intent.userId
      ? {
          kind: 'refused',
          error: new BatchConsoleError(409, 'account_changed', 'Another account answered.'),
        }
      : { kind: 'created', batch };
  if (lookupFirst) {
    try {
      const found = await deps.lookup(intent.key);
      if (found.state === 'created' && found.batch) return accept(found.batch);
      if (found.state === 'deleted') return { kind: 'deleted' };
    } catch (error) {
      return { kind: 'pending', error };
    }
  }
  const now = (deps.now ?? Date.now)();
  const windowMs = (deps.replaySeconds ?? DEFAULT_REPLAY_SECONDS) * 1000 - REPLAY_MARGIN_MS;
  if (now - intent.createdAt > windowMs) return { kind: 'stale' };
  try {
    return accept(await deps.create(intent.body, intent.key));
  } catch (error) {
    if (error instanceof BatchConsoleError && error.status === 410) return { kind: 'deleted' };
    if (isFinal(error)) return { kind: 'refused', error };
    return { kind: 'pending', error };
  }
}
