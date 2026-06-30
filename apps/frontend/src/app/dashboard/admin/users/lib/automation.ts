// Presentation helpers for the per-user automation score (human vs. script).

import type { UserAutomationScore } from '@/lib/api/admin';

export interface BandStyle {
  label: string;
  // Tailwind classes for a small pill/chip (background + text + ring).
  chip: string;
  // Tailwind text color for the numeric score.
  text: string;
}

// Maps a backend band id to its short label and color. HIGH score = more
// script-like, so the palette runs human (green) -> script (red).
const BANDS: Record<string, BandStyle> = {
  likely_human: {
    label: 'Human',
    chip: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    text: 'text-emerald-700',
  },
  mixed_or_uncertain: {
    label: 'Mixed',
    chip: 'bg-gray-100 text-gray-600 ring-gray-500/20',
    text: 'text-gray-600',
  },
  likely_automated: {
    label: 'Automated',
    chip: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    text: 'text-amber-700',
  },
  scripted_batch: {
    label: 'Script',
    chip: 'bg-red-50 text-red-700 ring-red-600/20',
    text: 'text-red-700',
  },
};

export function bandStyle(band: string): BandStyle {
  return BANDS[band] ?? BANDS.mixed_or_uncertain;
}

// Human-readable labels for the signals (keys match the backend signal names).
export const SIGNAL_LABELS: Record<string, string> = {
  turn_pattern: 'User turns',
  prompt_size_dispersion: 'Prompt size',
  user_message_shape: 'User messages',
  client_tool_prior: 'User-agent',
  daily_activity_shape: 'Daily activity',
  tool_call_human_tell: 'Tool use',
  agent_opener_override: 'Coding agent',
};

// Friendly labels for the supporting `detail` metrics.
export const DETAIL_LABELS: Record<string, string> = {
  one_shot_fraction: 'One-shot chat fraction',
  p90_user_turns: 'p90 user-turn depth',
  prompt_token_rcv: 'Prompt-size IQR/median',
  ua_base: 'UA-class base value',
  agent_share: 'Coding-agent opener share',
  hour_coverage: 'Active-hour coverage',
  hour_entropy_norm: 'Hour entropy (norm)',
  max_quiet_gap_hours: 'Longest quiet gap (h)',
  interarrival_rcv: 'Inter-arrival IQR/median',
  toolcall_share: 'Tool-call request share',
  user_msg_size_rcv: 'User-msg size IQR/median',
  user_msg_entropy: 'User-msg entropy (bits/char)',
  user_msg_distinct_ratio: 'User-msg distinct ratio',
};

// Sort comparator for ranking users by automation score. Users without a score
// (not yet computed, or no traffic) sort to the end regardless of direction.
export function compareByScore(
  a: UserAutomationScore | undefined,
  b: UserAutomationScore | undefined,
  dir: 'asc' | 'desc',
): number {
  const av = a?.score;
  const bv = b?.score;
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  return dir === 'desc' ? bv - av : av - bv;
}
