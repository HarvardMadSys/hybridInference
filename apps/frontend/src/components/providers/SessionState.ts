'use client';

import { useAuth, type AuthState } from './AuthProvider';

/**
 * What a *view* needs to know about the session, and nothing more.
 *
 * `useAuth()` hands back the whole controller: `login`, `logout`, `refreshUser`.
 * That is right for the shared pages, which own the forms and the redirect
 * rules, and wrong for anything that only has to decide which word to put on a
 * link. A distribution's site module is the second kind of consumer, and the
 * host facade exports this projection instead of the controller so the
 * difference is visible in the API rather than in a comment.
 *
 * The value is the controller's state object by reference, so a component that
 * migrated from `useAuth().state` to `useSession()` cannot observe a difference.
 */
export type SessionState = AuthState;

export function useSession(): SessionState {
  return useAuth().state;
}
