'use client';

export const PAGE_SIZE_OPTIONS = [50, 100, 250, 500] as const;

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
  const firstRow = total === 0 ? 0 : page * pageSize + 1;
  const lastRow = page * pageSize + count;
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
          <select
            className="rounded-md border border-gray-200 bg-white px-2 py-1 text-[13px] text-gray-900 focus:border-gray-400 focus:outline-none"
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            aria-label="Rows per page"
          >
            {PAGE_SIZE_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
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
