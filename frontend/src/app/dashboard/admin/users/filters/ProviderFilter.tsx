'use client';

const OPTIONS = ['', 'anthropic', 'minimax', 'ollama', 'zhipu'];

interface Props {
  value: string | null;
  onChange: (v: string | null) => void;
}

export function ProviderFilter({ value, onChange }: Props) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="rounded border bg-white px-2 py-1 text-sm"
    >
      {OPTIONS.map((p) => (
        <option key={p} value={p}>
          {p === '' ? 'Any provider' : p}
        </option>
      ))}
    </select>
  );
}
