/**
 * Tell this browser's other console tabs when its accounts change.
 *
 * The browser has one active account, shared by every tab (see
 * `serving/auth/browser_sessions.py`). A tab that signs in, switches or signs
 * out announces it here, so a tab still showing the previous account can stop
 * at once instead of discovering it at its next token refresh, and a tab with
 * an account chooser open can redraw it.
 *
 * BroadcastChannel never delivers a message to the tab that sent it. Where it
 * is unavailable nothing is announced and the refresh check still applies.
 */

const CHANNEL = 'hybridinference:accounts';

export type AccountSyncMessage =
  /** The browser's active account is now `userId`, or none. */
  | { type: 'active'; userId: string | null }
  /** The list of accounts changed; the active one did not. */
  | { type: 'accounts' };

// One channel object per tab, for sending and receiving alike. A message is
// delivered to every *other* object with the channel's name, including ones in
// the sending tab, so a second object here would hear this tab's own news.
let channel: BroadcastChannel | null | undefined;

function tabChannel(): BroadcastChannel | null {
  if (channel === undefined) {
    try {
      channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL);
    } catch {
      channel = null;
    }
  }
  return channel;
}

export function announceAccounts(message: AccountSyncMessage): void {
  try {
    tabChannel()?.postMessage(message);
  } catch {
    // A tab that cannot announce leaves the others to the refresh check.
  }
}

/** Call `handler` with every announcement from another tab; returns the unsubscribe. */
export function listenForAccounts(handler: (message: AccountSyncMessage) => void): () => void {
  const target = tabChannel();
  if (!target) return () => undefined;
  const onMessage = (event: MessageEvent<AccountSyncMessage>) => handler(event.data);
  target.addEventListener('message', onMessage);
  return () => target.removeEventListener('message', onMessage);
}
