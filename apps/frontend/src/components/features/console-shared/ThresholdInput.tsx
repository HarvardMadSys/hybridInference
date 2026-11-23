'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers/useT';

function toInputString(value: number | null): string {
  return value === null ? '' : String(value);
}

// An empty or non-finite text is no threshold, never NaN in the state or the address.
function parseInput(text: string): number | null {
  if (text === '') return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * A filter of a list by a least value: "Today ≥ $ [ ]". The label names the
 * figure and its unit; an empty field is no filter. The value goes to the
 * caller with each keystroke, and a value set from outside (a saved view, a
 * cleared filter) replaces what was typed.
 */
export function ThresholdInput({
  label,
  value,
  onChange,
  min = 0,
  step,
}: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  min?: number;
  step?: string;
}) {
  const t = useT();
  const [input, setInput] = useState(toInputString(value));

  useEffect(() => {
    setInput(toInputString(value));
  }, [value]);

  const handleChange = (text: string) => {
    setInput(text);
    const next = parseInput(text);
    if (next !== value) onChange(next);
  };

  return (
    <label className="flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white pl-2.5 pr-1 focus-within:border-gray-400">
      <span className="whitespace-nowrap text-[12px] text-gray-500">{label}</span>
      <input
        type="number"
        min={min}
        step={step}
        value={input}
        onChange={(event) => handleChange(event.target.value)}
        className="w-16 bg-transparent px-1 text-[13px] tabular-nums text-gray-900 placeholder:text-gray-300 focus:outline-none"
        placeholder={t('console.filter.any', 'any')}
      />
    </label>
  );
}
