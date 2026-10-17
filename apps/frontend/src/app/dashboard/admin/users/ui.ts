// Shared Tailwind class strings for the admin Users tab, so the filter bar,
// table and detail panel use one control height, one card style and one
// label style instead of a different ad-hoc size in every component.

/** Text inputs and selects in toolbars and forms. */
export const CONTROL =
  'h-8 rounded-md border border-gray-200 bg-white px-2.5 text-[13px] text-gray-900 placeholder:text-gray-400 focus:border-gray-400 focus:outline-none';

/** Small uppercase label above a field or a group of values. */
export const FIELD_LABEL = 'text-[11px] font-medium uppercase tracking-wide text-gray-500';

/** A white card section inside the expanded user panel. */
export const SECTION = 'rounded-lg border border-gray-200 bg-white p-4';

/** Primary (dark) and secondary (outlined) buttons. */
export const BUTTON_PRIMARY =
  'inline-flex items-center gap-1.5 rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white transition hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50';
export const BUTTON_SECONDARY =
  'inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50';
export const BUTTON_DANGER =
  'inline-flex items-center gap-1.5 rounded-md border border-red-200 bg-white px-3 py-1.5 text-[12px] font-medium text-red-600 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50';

/** Status pill per account status: label plus colors. */
export const STATUS_STYLE: Record<string, { label: string; pill: string; dot: string }> = {
  pending_approval: {
    label: 'Pending',
    pill: 'bg-indigo-50 text-indigo-700 ring-indigo-600/20',
    dot: 'bg-indigo-500',
  },
  active: {
    label: 'Active',
    pill: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    dot: 'bg-emerald-500',
  },
  suspended: {
    label: 'Suspended',
    pill: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    dot: 'bg-amber-500',
  },
  rejected: {
    label: 'Rejected',
    pill: 'bg-red-50 text-red-700 ring-red-600/20',
    dot: 'bg-red-500',
  },
  deleted: {
    label: 'Deleted',
    pill: 'bg-gray-100 text-gray-500 ring-gray-500/20',
    dot: 'bg-gray-400',
  },
};

/** Role badge colors; `free` is the default and gets no badge in the table. */
export const ROLE_STYLE: Record<string, string> = {
  free: 'bg-gray-100 text-gray-600',
  pro: 'bg-sky-50 text-sky-700',
  internal: 'bg-violet-50 text-violet-700',
  admin: 'bg-gray-900 text-white',
};
