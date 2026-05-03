// Shared types for the admin Users tab.
// UserRow mirrors backend UserListItem (decimals decoded as numbers/strings
// — keep numeric strings for safety and parse where needed).

export type UserStatus = 'pending_approval' | 'active' | 'suspended' | 'rejected' | 'deleted';

export type UserRole = 'free' | 'pro' | 'internal' | 'admin';

export interface UserRow {
  id: string;
  email: string;
  user_name: string | null;
  role: UserRole;
  status: UserStatus;
  email_verified: boolean;
  approval_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  usage_today_usd: string; // Decimal serialised
  usage_month_usd: string;
  usage_alltime_usd: string;
}

export interface CostHistoryPoint {
  day: string; // YYYY-MM-DD
  cost_usd: string; // Decimal
  requests: number;
}

export interface SummaryUserItem {
  id: string;
  email: string;
  user_name: string | null;
  role: UserRole;
  today_cost_usd: string;
  avg_prior_7d_usd: string;
  quota_daily_usd: number | null;
  multiplier: number | null;
}

export interface SummaryCard {
  count: number;
  top: SummaryUserItem[];
}

export interface UsersSummary {
  pending: SummaryCard;
  top_spenders_today: SummaryCard;
  anomalies: SummaryCard;
  near_quota: SummaryCard;
}

export type SortBy = 'created' | 'cost_today' | 'cost_month' | 'cost_alltime' | 'last_login';

export type QuotaStateFilter = 'near' | 'over' | 'custom' | 'default';

export interface FilterState {
  status: UserStatus | null;
  search: string;
  sortBy: SortBy;
  minCostToday: number | null;
  minCostMonth: number | null;
  quotaState: QuotaStateFilter | null;
  provider: string | null;
  activeWithinHours: number | null;
  anomaly: boolean | null;
  view: string | null; // built-in or saved-view id
}

export interface SavedView {
  id: string; // slug
  name: string;
  builtin: boolean;
  filterState: FilterState;
}

export type Density = 'comfortable' | 'compact';
