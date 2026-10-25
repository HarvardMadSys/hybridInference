import { useEffect } from 'react';

/**
 * Tell a parent whether this editor has an edit or a confirmation open, so the
 * parent can keep navigation that would drop the drafts (switching the selected
 * model, say) locked until it is saved or cancelled. Reports `false` again when
 * the editor goes away.
 */
export function useReportEditing(
  active: boolean,
  onEditingChange: ((active: boolean) => void) | undefined,
): void {
  useEffect(() => {
    onEditingChange?.(active);
  }, [active, onEditingChange]);

  useEffect(() => () => onEditingChange?.(false), [onEditingChange]);
}
