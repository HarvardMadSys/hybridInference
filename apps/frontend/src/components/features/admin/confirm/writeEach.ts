import { getErrorMessage } from '@/lib/utils/errors';

import { plural } from './ChangeList';

/**
 * Run a confirmed save that needs one request per item, in order.
 *
 * - When the first request fails nothing was written, so this throws: the
 *   confirmation stays open with the error and Confirm can safely retry.
 * - When a later one fails, the earlier writes stand. This returns a message
 *   that names the failed item and says how many were saved, and stops: the
 *   caller closes the review and keeps only the unsaved drafts, so a retry is
 *   reviewed afresh and never replays a write that already succeeded.
 * - `null` means every request succeeded.
 *
 * `write` must itself drop the item's draft once its request succeeds.
 */
export async function writeEach<T>(
  items: readonly T[],
  name: (item: T) => string,
  write: (item: T) => Promise<void>,
): Promise<string | null> {
  let saved = 0;
  for (const item of items) {
    try {
      await write(item);
      saved += 1;
    } catch (e) {
      const reason = `${name(item)} could not be saved: ${getErrorMessage(e)}`;
      if (saved === 0) throw new Error(reason);
      return `${reason} ${plural(saved, 'change')} ${saved === 1 ? 'was' : 'were'} saved; the rest are still unsaved.`;
    }
  }
  return null;
}
