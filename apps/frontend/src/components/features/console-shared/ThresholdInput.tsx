'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers/useT';

function toInputString(value: number | null): string {
  return value === null ? '' : String(value);
}

// An empty or non-finite text is no threshold, never NaN in the state or the
// address; nor is a number below the least one the list takes, which the
// gateway would refuse.
function parseInput(text: string, min: number): number | null {
  if (text === '') return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) && parsed >= min ? parsed : null;
}

/** Whether the text is a number that the list does not take: below its least value. */
function belowMin(text: string, min: number): boolean {
  const parsed = Number(text);
  return text !== '' && Number.isFinite(parsed) && parsed < min;
}

/**
 * A filter of a list by a least value: "Today ≥ $ [ ]". The label names the
 * figure and its unit; an empty field is no filter. The value goes to the
 * caller with each keystroke, and a value set from outside (a saved view, a
 * cleared filter) replaces what was typed. A number below `min` is no filter
 * either: the field is marked, and nothing is sent for it.
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

  // A value from outside replaces the text, unless the text already stands for
  // it: what the reader typed stays as typed ("2." for 2, "-1" for no filter).
  useEffect(() => {
    setInput((typed) => (parseInput(typed, min) === value ? typed : toInputString(value)));
  }, [value, min]);

  const handleChange = (text: string) => {
    setInput(text);
    const next = parseInput(text, min);
    if (next !== value) onChange(next);
  };

  const invalid = belowMin(input, min);
  return (
    <label
      className={`flex h-8 items-center gap-1.5 rounded-md border bg-white pl-2.5 pr-1 ${
        invalid ? 'border-red-300' : 'border-gray-200 focus-within:border-gray-400'
      }`}
    >
      <span className="whitespace-nowrap text-[12px] text-gray-500">{label}</span>
      <input
        type="number"
        min={min}
        step={step}
        value={input}
        aria-invalid={invalid || undefined}
        onChange={(event) => handleChange(event.target.value)}
        className="w-16 bg-transparent px-1 text-[13px] tabular-nums text-gray-900 placeholder:text-gray-300 focus:outline-none"
        placeholder={t('console.filter.any', 'any')}
      />
    </label>
  );
}
