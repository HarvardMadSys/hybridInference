import { useEffect } from 'react';

/**
 * Tell the page how many changes a section holds unsaved, whenever that
 * changes, so a sub-tab that is not on screen can still say it has some.
 */
export function useReportUnsaved(count: number, onUnsavedChange?: (count: number) => void) {
  useEffect(() => {
    onUnsavedChange?.(count);
  }, [count, onUnsavedChange]);
}
