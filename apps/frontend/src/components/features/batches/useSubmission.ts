'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  createConsoleBatch,
  findSubmission,
  type BatchConsoleError,
  type BatchDetail,
  type ConsoleSubmission,
} from '@/lib/api/batches';
import {
  newIntent,
  settleIntent,
  withIntentStore,
  type IntentOutcome,
  type IntentStore,
  type SubmitIntent,
} from '@/lib/batches/submitIntent';

/** Waits between automatic retries of a submission whose answer is not known yet. */
const RETRY_DELAYS_MS = [2000, 5000, 10000, 30000, 60000];

export type SubmissionState =
  | { phase: 'idle' }
  /** A request (or a recovery) is running. */
  | { phase: 'sending'; intent: SubmitIntent | null }
  /** The answer is not known yet; the same submission is tried again by itself. */
  | { phase: 'confirming'; intent: SubmitIntent; error: unknown }
  /** Too old to send again safely; waits for the user's decision. */
  | { phase: 'stale'; intent: SubmitIntent }
  | { phase: 'created'; batch: BatchDetail }
  | { phase: 'deleted' }
  | { phase: 'refused'; error: BatchConsoleError }
  /** The browser cannot coordinate tabs: nothing is submitted. */
  | { phase: 'unsupported' };

export interface Submission {
  state: SubmissionState;
  /** Submit `body` as one new submission (unless one is still pending: that one is settled). */
  submit: (body: ConsoleSubmission) => Promise<void>;
  /**
   * For the stale submission `shownKey` the user saw: make a deliberate new one
   * with the same content. If another tab changed the pending submission
   * meanwhile, that one is recovered instead; nothing is replaced unseen.
   */
  submitAsNew: (shownKey: string) => Promise<void>;
  /** For the stale submission `shownKey`: forget it (only it) without submitting anything. */
  discard: (shownKey: string) => Promise<void>;
  /** Back to idle after a refusal the page has shown. */
  reset: () => void;
}

/**
 * The account's console submission: one intent at a time, recovered on load.
 *
 * Every step runs under the account's intent lock; the intent is cleared only
 * once its outcome is certain. See `@/lib/batches/submitIntent`.
 */
export function useSubmission(userId: string | null, replaySeconds?: number): Submission {
  const [state, setState] = useState<SubmissionState>({ phase: 'idle' });
  const generation = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attempts = useRef(0);
  // The current account's recovery, for the retry timer (never a previous account's).
  const recoverRef = useRef<(mine: number) => Promise<void>>(async () => {});

  const deps = useCallback(
    () => ({ lookup: findSubmission, create: createConsoleBatch, replaySeconds }),
    [replaySeconds],
  );

  const apply = useCallback(
    async (
      mine: number,
      intent: SubmitIntent,
      outcome: IntentOutcome,
      clear: () => Promise<void>,
    ) => {
      if (mine !== generation.current) return;
      if (outcome.kind === 'pending') {
        setState({ phase: 'confirming', intent, error: outcome.error });
        const delay = RETRY_DELAYS_MS[Math.min(attempts.current, RETRY_DELAYS_MS.length - 1)];
        attempts.current += 1;
        timer.current = setTimeout(() => void recoverRef.current(mine), delay);
        return;
      }
      if (outcome.kind === 'stale') {
        setState({ phase: 'stale', intent });
        return;
      }
      await clear();
      attempts.current = 0;
      if (outcome.kind === 'created') setState({ phase: 'created', batch: outcome.batch });
      else if (outcome.kind === 'deleted') setState({ phase: 'deleted' });
      else setState({ phase: 'refused', error: outcome.error });
    },
    [],
  );

  const run = useCallback(
    async (mine: number, step: (owner: string) => Promise<void>) => {
      if (!userId) return;
      try {
        await step(userId);
      } catch (error) {
        if (mine !== generation.current) return;
        if ((error as Error)?.name === 'IntentUnsupportedError') setState({ phase: 'unsupported' });
        else throw error;
      }
    },
    [userId],
  );

  const recover = useCallback(
    async (mine: number) => {
      await run(mine, (owner) =>
        withIntentStore(owner, async (store) => {
          if (mine !== generation.current) return;
          const intent = await store.get(owner);
          if (mine !== generation.current) return;
          if (!intent) {
            setState((current) =>
              current.phase === 'confirming' || current.phase === 'sending'
                ? { phase: 'idle' }
                : current,
            );
            return;
          }
          setState({ phase: 'sending', intent });
          const outcome = await settleIntent(intent, deps());
          await apply(mine, intent, outcome, () => store.clear(owner, intent.key));
        }),
      );
    },
    [apply, deps, run],
  );
  recoverRef.current = recover;

  useEffect(() => {
    // A new account (or none): forget everything shown for the previous one,
    // then recover this account's own pending submission, if any.
    const mine = ++generation.current;
    attempts.current = 0;
    if (timer.current) clearTimeout(timer.current);
    setState({ phase: 'idle' });
    if (userId) void recover(mine);
    return () => {
      generation.current += 1;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [userId, recover]);

  /** Settle `intent` and apply its outcome, unless the hook moved on meanwhile. */
  const settleAndApply = useCallback(
    async (
      mine: number,
      owner: string,
      store: IntentStore,
      intent: SubmitIntent,
      lookupFirst: boolean,
    ) => {
      if (mine !== generation.current) return;
      setState({ phase: 'sending', intent });
      const outcome = await settleIntent(intent, deps(), { lookupFirst });
      await apply(mine, intent, outcome, () => store.clear(owner, intent.key));
    },
    [apply, deps],
  );

  const submit = useCallback(
    async (body: ConsoleSubmission) => {
      const mine = generation.current;
      await run(mine, (owner) =>
        withIntentStore(owner, async (store) => {
          // Waiting for the lock may outlast this account on the page.
          if (mine !== generation.current) return;
          const fresh = newIntent(owner, body);
          const intent = await store.createIfAbsent(fresh);
          // Another tab's submission still pending: settle that one instead.
          await settleAndApply(mine, owner, store, intent, intent.key !== fresh.key);
        }),
      );
    },
    [run, settleAndApply],
  );

  const submitAsNew = useCallback(
    async (shownKey: string) => {
      const mine = generation.current;
      await run(mine, (owner) =>
        withIntentStore(owner, async (store) => {
          if (mine !== generation.current) return;
          const current = await store.get(owner);
          if (!current) {
            if (mine === generation.current) setState({ phase: 'idle' });
            return;
          }
          if (current.key !== shownKey) {
            // Another tab acted on the stale submission first: what is pending
            // now is its choice, recovered rather than replaced.
            await settleAndApply(mine, owner, store, current, true);
            return;
          }
          const next = newIntent(owner, current.body);
          const stored = await store.replaceIf(owner, shownKey, next);
          if (!stored) {
            if (mine === generation.current) setState({ phase: 'idle' });
            return;
          }
          await settleAndApply(mine, owner, store, stored, stored.key !== next.key);
        }),
      );
    },
    [run, settleAndApply],
  );

  const discard = useCallback(
    async (shownKey: string) => {
      const mine = generation.current;
      await run(mine, (owner) =>
        withIntentStore(owner, async (store) => {
          if (mine !== generation.current) return;
          // Only the submission the user saw; one another tab made since stays.
          await store.clear(owner, shownKey);
          const current = await store.get(owner);
          if (current) {
            await settleAndApply(mine, owner, store, current, true);
          } else if (mine === generation.current) {
            setState({ phase: 'idle' });
          }
        }),
      );
    },
    [run, settleAndApply],
  );

  const reset = useCallback(() => setState({ phase: 'idle' }), []);

  return { state, submit, submitAsNew, discard, reset };
}
