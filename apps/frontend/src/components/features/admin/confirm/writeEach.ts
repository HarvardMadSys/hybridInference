import { getErrorMessage } from '@/lib/utils/errors';

import { plural } from './ChangeList';

/**
 * Run a save that needs one request per item, in order, and stop at the first
 * one that fails.
 *
 * `write` must itself drop the item's draft once its request succeeds: what is
 * left after a failure is then exactly what is unsaved, and saving again never
 * replays a write that already succeeded. The error thrown names the failed
 * item and, when earlier ones were written, how many were saved.
 */
export async function writeEach<T>(
  items: readonly T[],
  name: (item: T) => string,
  write: (item: T) => Promise<void>,
): Promise<void> {
  let saved = 0;
  for (const item of items) {
    try {
      await write(item);
      saved += 1;
    } catch (e) {
      const reason = `${name(item)} could not be saved: ${getErrorMessage(e)}`;
      throw new Error(
        saved === 0
          ? reason
          : `${reason} ${plural(saved, 'change')} ${saved === 1 ? 'was' : 'were'} saved; the rest are still unsaved.`,
      );
    }
  }
}
