// Shared Tailwind class strings for the admin console, so every tab uses one
// control height, one card style, one label style and one button set instead
// of a different ad-hoc size in every component.
//
// Only classes the console's dark theme remaps (see styles/console-theme.css)
// carry color here: a `disabled:bg-*` or `focus:ring-gray-900/*` variant has no
// dark counterpart and would show a light patch or vanish on a dark canvas.

const FIELD_BASE =
  'rounded-md border border-gray-200 bg-white px-2.5 text-[13px] text-gray-900 placeholder:text-gray-400 transition focus:border-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-400/30 disabled:cursor-not-allowed disabled:opacity-60';

/** Text inputs and selects in toolbars and forms, at the toolbar's height. */
export const CONTROL = `h-8 ${FIELD_BASE}`;

/** A full-width form input or textarea; grows with its content. */
export const TEXT_FIELD = `block w-full py-1.5 ${FIELD_BASE}`;

/** Border for a field whose value is invalid; append after the field class. */
export const FIELD_INVALID = 'border-red-300 focus:border-red-400 focus:ring-red-400/30';

/** Small uppercase label above a field or a group of values. */
export const FIELD_LABEL = 'text-[11px] font-medium uppercase tracking-wide text-gray-500';

/** Label directly above a form control. */
export const FORM_LABEL = 'mb-1 block text-[12px] font-medium text-gray-700';

/** Help text below a form control. */
export const FIELD_HINT = 'mt-1 text-[11px] text-gray-500';

/** Checkbox and radio inputs. */
export const CHECKBOX = 'h-4 w-4 shrink-0 rounded border-gray-300 disabled:opacity-60';

/** A white card section inside an expanded panel. */
export const SECTION = 'rounded-lg border border-gray-200 bg-white p-4';

/** A top-level card on an admin tab: tables, forms and feeds sit in one. */
export const CARD = 'rounded-xl border border-gray-200 bg-white shadow-sm';

/** The header strip of a card that holds a toolbar (search, filters). */
export const CARD_TOOLBAR =
  'flex flex-wrap items-center gap-2 rounded-t-xl border-b border-gray-200 bg-gray-50/50 p-3';

/** Table header cell and body cell, for the admin's data tables. */
export const TABLE_HEAD_CELL =
  'px-4 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-gray-500';
export const TABLE_CELL = 'px-4 py-2.5 text-[13px] text-gray-700';

/** Primary (dark), secondary (outlined), danger and quiet text buttons. */
export const BUTTON_PRIMARY =
  'inline-flex items-center gap-1.5 rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white transition hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50';
export const BUTTON_SECONDARY =
  'inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[12px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50';
export const BUTTON_DANGER =
  'inline-flex items-center gap-1.5 rounded-md border border-red-200 bg-white px-3 py-1.5 text-[12px] font-medium text-red-600 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50';
export const BUTTON_GHOST =
  'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50';

/** A text-style action inside an inline notice, e.g. Retry. */
export const NOTICE_ACTION = 'shrink-0 font-medium underline underline-offset-2';

/** Small rounded status pill; add a background and text color. */
export const PILL = 'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium';
