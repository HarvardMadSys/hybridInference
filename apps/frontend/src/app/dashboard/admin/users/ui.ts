// Class strings for the admin Users tab. The control, card and button styles
// are the admin console's shared ones; only the status and role colors below
// are the Users tab's own.

export {
  BUTTON_DANGER,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CONTROL,
  FIELD_LABEL,
  SECTION,
} from '@/components/features/admin/adminStyles';

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
