'use client';

import { useT } from '@/components/providers/useT';
import type { ConsoleBatch } from '@/lib/api/batches';

import { batchCategory, CATEGORY_TONE, categoryLabel, categoryNote } from './batchText';

/** A batch's category badge, with the short note that says what it leaves out. */
export function BatchCategoryBadge({ batch }: { batch: ConsoleBatch }) {
  const t = useT();
  const category = batchCategory(batch);
  const note = categoryNote(batch, t);
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-testid="batch-category">
      <span
        className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${CATEGORY_TONE[category]}`}
        data-category={category}
      >
        {categoryLabel(category, t)}
      </span>
      {note && <span className="text-xs text-gray-600">{note}</span>}
    </span>
  );
}
