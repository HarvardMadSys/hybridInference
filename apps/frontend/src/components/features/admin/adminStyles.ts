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

/**
 * The one box a list or table sits in: a hairline border and no shadow, like
 * the Users table. Section titles and toolbars stay outside it.
 */
export const PANEL = 'rounded-lg border border-gray-200 bg-white';

/** Two columns on wide screens: a section index, then the sections. */
export const INDEXED_LAYOUT = 'lg:grid lg:grid-cols-[11rem_minmax(0,1fr)] lg:gap-8';

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

/**
 * Quiet text actions for a row (Edit, Publish) and their destructive
 * counterpart: visible, but lighter than the page's one primary button.
 */
const ROW_ACTION_BASE =
  'inline-flex items-center rounded-md px-2 py-1 text-[12px] font-medium transition disabled:cursor-not-allowed disabled:opacity-50';
export const ROW_ACTION = `${ROW_ACTION_BASE} text-gray-600 hover:bg-gray-100 hover:text-gray-900`;
export const ROW_ACTION_DANGER = `${ROW_ACTION_BASE} text-red-600 hover:bg-red-50 hover:text-red-700`;

/**
 * Rendered Markdown (an announcement body, an email preview). Long words and
 * URLs wrap, and a wide table or code block scrolls inside the text rather
 * than widening the panel it sits in; the text itself is untouched.
 */
export const MARKDOWN_PROSE =
  'min-w-0 max-w-full text-[13px] text-gray-700 [overflow-wrap:anywhere] [&_a]:text-blue-600 [&_a]:underline [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_pre]:my-2 [&_pre]:max-w-full [&_pre]:relative [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-black/5 [&_pre]:p-2 [&_pre]:[overflow-wrap:normal] [&_table]:my-2 [&_table]:block [&_table]:max-w-full [&_table]:relative [&_table]:overflow-x-auto [&_table]:[overflow-wrap:normal] [&_td]:px-2 [&_td]:py-1 [&_th]:px-2 [&_th]:text-left [&_th]:py-1 [&_img]:max-w-full';

/** A text-style action inside an inline notice, e.g. Retry. */
export const NOTICE_ACTION = 'shrink-0 font-medium underline underline-offset-2';

/** Small rounded status pill; add a background and text color. */
export const PILL = 'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium';
