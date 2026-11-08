'use client';

import { useState, type MutableRefObject } from 'react';

/**
 * The administrator's password, inside a confirmation dialog. The dialog's body
 * is fixed when it opens, so the field keeps its own state and hands the value
 * to the write through `valueRef`.
 */
export function ReauthPassword({
  valueRef,
  label,
}: {
  valueRef: MutableRefObject<string>;
  label: string;
}) {
  const [value, setValue] = useState('');
  return (
    <label className="block text-[12px] text-gray-700">
      {label}
      <input
        type="password"
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          valueRef.current = event.target.value;
        }}
        autoComplete="current-password"
        className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
      />
    </label>
  );
}
