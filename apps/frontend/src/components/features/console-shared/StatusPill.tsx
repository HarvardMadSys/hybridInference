/** A status as a list shows it: its word, and the colors of its pill and its dot. */
export interface StatusTone {
  label: string;
  pill: string;
  dot: string;
}

/** The pill of an account's status, or of a request's outcome: a dot and a word. */
export function StatusPill({ tone }: { tone: StatusTone }) {
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${tone.pill}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} aria-hidden="true" />
      {tone.label}
    </span>
  );
}
