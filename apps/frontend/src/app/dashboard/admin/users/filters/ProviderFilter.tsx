'use client';

const OPTIONS = [
  '',
  'anthropic',
  'chutes',
  'deepseek',
  'featherless',
  'minimax',
  'ollama',
  'openrouter',
  'zai',
];

const LABELS: Record<string, string> = {
  zai: 'Z.AI',
  deepseek: 'DeepSeek',
  chutes: 'Chutes',
  featherless: 'Featherless',
  openrouter: 'OpenRouter',
  minimax: 'Minimax',
};

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
          {p === '' ? 'Any provider' : (LABELS[p] ?? p)}
        </option>
      ))}
    </select>
  );
}
