'use client';

import { useMoney } from '@/lib/hooks/useMoney';
import { useEffect, useRef, useState } from 'react';
import { COLUMN_GROUPS, type ColumnId } from './lib/columns';

interface ColumnPickerProps {
  hidden: ColumnId[];
  onChange: (hidden: ColumnId[]) => void;
}

/** "Columns" menu: checkboxes for the optional metric columns, by group. */
export function ColumnPicker({ hidden, onChange }: ColumnPickerProps) {
  const money = useMoney();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const toggle = (id: ColumnId) =>
    onChange(hidden.includes(id) ? hidden.filter((h) => h !== id) : [...hidden, id]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="true"
        className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition ${
          open || hidden.length > 0
            ? 'border-gray-300 bg-white text-gray-900'
            : 'border-gray-200 bg-white text-gray-600 hover:text-gray-900'
        }`}
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path d="M2 4.75A.75.75 0 012.75 4h14.5a.75.75 0 010 1.5H2.75A.75.75 0 012 4.75zM2 10a.75.75 0 01.75-.75h14.5a.75.75 0 010 1.5H2.75A.75.75 0 012 10zm0 5.25a.75.75 0 01.75-.75h14.5a.75.75 0 010 1.5H2.75a.75.75 0 01-.75-.75z" />
        </svg>
        Columns
        {hidden.length > 0 && (
          <span className="rounded-full bg-gray-100 px-1.5 text-[10px] font-semibold tabular-nums text-gray-600">
            {hidden.length} hidden
          </span>
        )}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-1 w-56 rounded-lg border border-gray-200 bg-white p-2 shadow-lg"
        >
          {COLUMN_GROUPS.map((group) => (
            <div
              key={group.label === 'Spend (USD)' ? `Spend (${money.currency})` : group.label}
              className="py-1"
            >
              <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                {group.label === 'Spend (USD)' ? `Spend (${money.currency})` : group.label}
              </div>
              {group.columns.map((col) => (
                <label
                  key={col.id}
                  className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-[13px] text-gray-700 hover:bg-gray-50"
                >
                  <input
                    type="checkbox"
                    checked={!hidden.includes(col.id)}
                    onChange={() => toggle(col.id)}
                  />
                  {col.label}
                </label>
              ))}
            </div>
          ))}
          <div className="mt-1 border-t border-gray-100 px-2 pt-2">
            <button
              type="button"
              onClick={() => onChange([])}
              disabled={hidden.length === 0}
              className="text-[12px] font-medium text-gray-500 transition hover:text-gray-900 disabled:opacity-40"
            >
              Show all columns
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
