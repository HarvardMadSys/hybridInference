'use client';

import { Select } from '@/components/ui/Select';

export const PAGE_SIZE_OPTIONS = [50, 100, 250, 500] as const;

const PAGE_SIZE_ITEMS = PAGE_SIZE_OPTIONS.map((size) => ({
  value: String(size),
  label: String(size),
}));

interface PaginationProps {
  /** Zero-based page index. */
  page: number;
  pageSize: number;
  /** Total matching rows (across all pages), from the API `total` field. */
  total: number;
  /** Number of rows on the current page (may be < pageSize on the last page). */
  count: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

/**
 * Pagination footer for the admin Users table.
 *
 * The backend `/admin/users` endpoint pages via limit/offset and returns the
 * unfiltered-by-page `total`, so we can show an accurate range and page count
 * even though only one page of rows is loaded at a time.
 */
export function Pagination({
  page,
  pageSize,
  total,
  count,
  onPageChange,
  onPageSizeChange,
}: PaginationProps) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  // When the page is briefly out of range (a mutation shrank `total` below the
  // current offset, before the parent's clamp effect resets `page`), the
  // current page has no rows. Guard the range so it never renders reversed
  // numbers like "Showing 201–200"; show 0 rows until the clamp lands.
  const hasRows = count > 0;
  const firstRow = hasRows ? page * pageSize + 1 : 0;
  const lastRow = hasRows ? page * pageSize + count : 0;
  const canPrev = page > 0;
  const canNext = page < pageCount - 1;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 px-1 py-2 text-[13px] text-gray-600">
      <div className="flex items-center gap-2">
        <span>
          {total === 0 ? (
            'No users'
          ) : (
            <>
              Showing <span className="font-medium text-gray-900">{firstRow}</span>–
              <span className="font-medium text-gray-900">{lastRow}</span> of{' '}
              <span className="font-medium text-gray-900">{total}</span>
            </>
          )}
        </span>
        <label className="ml-3 flex items-center gap-1.5 text-gray-500">
          <span>Per page</span>
          <Select
            size="sm"
            className="w-20"
            value={String(pageSize)}
            onValueChange={(v) => onPageSizeChange(Number(v))}
            options={PAGE_SIZE_ITEMS}
            aria-label="Rows per page"
          />
        </label>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-gray-500">
          Page <span className="font-medium text-gray-900">{page + 1}</span> of {pageCount}
        </span>
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={!canPrev}
          className="rounded-md border border-gray-200 px-2.5 py-1 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={!canNext}
          className="rounded-md border border-gray-200 px-2.5 py-1 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  );
}
