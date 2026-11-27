'use client';

import { CONTROL } from '@/components/features/admin/adminStyles';

/**
 * The search field of a list, with the magnifier inside it, at the height of
 * the toolbar it stands in. The caller holds the text and decides when the
 * list is asked for again.
 */
export function SearchBox({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  /** The accessible name, where the placeholder is not one. */
  label?: string;
}) {
  return (
    <div className="relative min-w-[14rem] flex-1">
      <svg
        className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path
          fillRule="evenodd"
          d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
          clipRule="evenodd"
        />
      </svg>
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className={`${CONTROL} w-full pl-8`}
      />
    </div>
  );
}
