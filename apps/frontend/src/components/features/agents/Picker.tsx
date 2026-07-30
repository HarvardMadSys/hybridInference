'use client';

// A composer control that actually selects something.
//
// These were static buttons: the row showed a repository, a branch, a runtime
// and a model, and the Run handler submitted its own hardcoded values. So the
// screen described one job and queued another — worse than an obviously
// disabled control, because nothing looked wrong.
export function Picker({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (next: string) => void;
}) {
  if (options.length === 0) {
    return null;
  }
  return (
    <label className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100">
      <span className="sr-only">{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="cursor-pointer border-0 bg-transparent p-0 pr-5 text-[13px] font-medium text-gray-700 focus:outline-none focus:ring-0"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}
