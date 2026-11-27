import { fetchWithAuth } from './client';
import { readJson } from './detail';
import { config } from '@/config/env';
import type { Currency } from '@/lib/utils/money';

/**
 * The organization console: what an organization administrator sees of the
 * organization's members and can change.
 *
 * No call names the organization. The gateway takes it from the membership of
 * the account that asks, and answers 403 to an account that is not an
 * administrator of an organization.
 *
 * Money follows the accounting contract: the `*_usd` fields, and on a ledger
 * that is not in USD the native amounts under the same names without the
 * suffix, with `currency`. Read an amount with `moneyAmount(source, field)`.
 *
 * A refusal keeps the sentence of the gateway, for the page to show as it is
 * (`readJson`). `jsonOrThrow` is for the sign-in pages: it reads every 409 as
 * "This email is already registered".
 */

const API_BASE = config.apiBase;
const ROOT = '/user/organization';

export type OrganizationPeriod = '1d' | '7d' | '30d';
export type OrganizationRole = 'member' | 'admin';

/** The organization of an account, on the account's own profile (`GET /user/me`). */
export interface OrganizationRef {
  id: string;
  name: string;
  role: OrganizationRole;
}

interface Money {
  /** The ledger currency of the native amounts, on a ledger that is not in USD. */
  currency?: Currency;
}

/** One SSO connection of the organization; `available` is false when its provider entry is gone. */
export interface OrganizationConnection {
  provider_id: string;
  available: boolean;
  display_name?: string | null;
  type?: string | null;
  domains: string[];
  enabled: boolean;
  auto_create: boolean;
  require_approval: boolean;
}

export interface OrganizationAdministrator {
  user_id: string;
  email?: string | null;
  user_name?: string | null;
  last_login_at?: string | null;
}

export interface Organization extends Money {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
  /** `null` is no limit. */
  monthly_limit_usd?: number | null;
  monthly_limit?: number | null;
  /**
   * What a member gets that the organization set no limit for; `null` is none.
   * Not an administrator, of the organization or of the platform.
   */
  default_member_monthly_limit_usd?: number | null;
  default_member_monthly_limit?: number | null;
  /** What the members were charged in this UTC month, as the limit counts it. */
  spent_month_usd: number;
  spent_month?: number | null;
  connections: OrganizationConnection[];
  administrators: OrganizationAdministrator[];
}

/**
 * Which limit applies to a member: the one its organization set for it, the
 * organization's default, the quota the platform administrator set for the
 * account, or none. The gateway enforces the lower of the organization's and
 * the platform administrator's.
 */
export type MemberLimitSource = 'member' | 'default' | 'platform' | 'none';

/** What a member spent on one UTC day (`YYYY-MM-DD`). */
export interface SpendDay extends Money {
  day: string;
  cost_usd: number;
  cost?: number | null;
}

export interface OrganizationMember extends Money {
  user_id: string;
  email?: string | null;
  user_name?: string | null;
  role: OrganizationRole;
  /**
   * An administrator, of the organization or of the platform: the organization
   * sets no limit of it and does not remove it. Absent from a gateway that
   * does not send it yet.
   */
  protected?: boolean;
  /** The account's status. */
  status: string;
  /** The organization removed this member, so the organization can restore it. */
  removed: boolean;
  joined_at: string;
  last_login_at?: string | null;
  last_request_at?: string | null;
  requests: number;
  total_tokens: number;
  cost_usd: number;
  cost?: number | null;
  spent_month_usd: number;
  spent_month?: number | null;
  /**
   * Today, the last 7 days and all time, of the whole account: the figures
   * that Admin shows for it. They are the same for every period. Absent from
   * a gateway that does not send them yet.
   */
  spent_today_usd?: number;
  spent_today?: number | null;
  spent_7d_usd?: number;
  spent_7d?: number | null;
  spent_alltime_usd?: number;
  spent_alltime?: number | null;
  /** The spend of each of the last 7 UTC days, the oldest first, with today. */
  spend_days?: SpendDay[];
  /** The limit that applies, and which one it is. */
  monthly_limit_usd?: number | null;
  monthly_limit?: number | null;
  monthly_limit_source: MemberLimitSource;
  /** The limit the organization set for this member: what its administrator edits. */
  own_monthly_limit_usd?: number | null;
  own_monthly_limit?: number | null;
  /**
   * The monthly quota the platform administrator set for the account. The
   * organization reads it and cannot change it.
   */
  platform_monthly_limit_usd?: number | null;
  platform_monthly_limit?: number | null;
}

export interface MemberList {
  total: number;
  members: OrganizationMember[];
}

/** One point of a series: a UTC day, or an hour for the `1d` period. */
export interface UsagePoint extends Money {
  bucket: string;
  cost_usd: number;
  cost?: number | null;
  requests: number;
  total_tokens: number;
  members_with_requests: number;
}

export interface ModelUsage extends Money {
  model_id: string;
  requests: number;
  errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** The input tokens that the provider read from its cache. */
  cached_tokens?: number;
  total_tokens?: number;
  cost_usd: number;
  cost?: number | null;
}

export interface PeriodTotals extends Money {
  cost_usd: number;
  cost?: number | null;
  requests: number;
}

export interface OrganizationOverview extends Money {
  period: OrganizationPeriod;
  bucket: 'day' | 'hour';
  cost_usd: number;
  cost?: number | null;
  requests: number;
  total_tokens: number;
  members_with_requests: number;
  member_count: number;
  /** The period of the same length before this one; absent for `30d`. */
  previous?: PeriodTotals | null;
  spent_month_usd: number;
  spent_month?: number | null;
  monthly_limit_usd?: number | null;
  monthly_limit?: number | null;
  series: UsagePoint[];
  models: ModelUsage[];
  /** The members with the highest spend in the period, the highest first. */
  top_members?: OrganizationMember[];
}

/** How many members a card above the list stands for, and the first of them. */
export interface MemberSummaryCard {
  count: number;
  top: OrganizationMember[];
}

/**
 * The two cards above the member list: the members at 80% or more of the
 * monthly limit that applies to them, the highest share first, and the members
 * that spent anything today, the highest spend first.
 */
export interface MembersSummary {
  near_limit: MemberSummaryCard;
  top_spenders_today: MemberSummaryCard;
}

/** The requests of one window that ends now. A request failed with a status of 400 or more. */
export interface RequestVolumeWindow {
  requests: number;
  ok: number;
  failed: number;
  /** `null` when no request of the window recorded a latency. */
  avg_latency_ms?: number | null;
}

/** The requests of the members in the last hour, day, week and month (30 days). */
export interface RequestVolume {
  hour: RequestVolumeWindow;
  day: RequestVolumeWindow;
  week: RequestVolumeWindow;
  month: RequestVolumeWindow;
}

/** One request of a member. The gateway never sends its content, nor a key's value. */
export interface OrganizationRequest extends Money {
  id: number;
  timestamp: string;
  user_id: string;
  email?: string | null;
  user_name?: string | null;
  project_name?: string | null;
  key_name?: string | null;
  model_id: string;
  status_code?: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  cost?: number | null;
}

export interface RequestPage {
  requests: OrganizationRequest[];
  /** Pass as `before` for the page after this one; `null` on the last page. */
  next_before?: string | null;
}

/** What the account of a member has: an active API key, a second factor, a verified email. */
export interface MemberAccount {
  has_key: boolean;
  two_factor_enabled: boolean;
  email_verified: boolean;
}

/** A project of a member: its name and what it spent, never its budgets or its models. */
export interface MemberProject extends Money {
  id: string;
  name: string;
  is_default: boolean;
  created_at: string;
  spent_today_usd: number;
  spent_today?: number | null;
  spent_month_usd: number;
  spent_month?: number | null;
}

/**
 * An API key of a member by its name. The gateway sends no value, no prefix
 * and no limit of a key; `id` names the key to `revokeMemberKey`.
 */
export interface MemberKey extends Money {
  id: number;
  name?: string | null;
  status: string;
  project_id?: string | null;
  project_name?: string | null;
  created_at: string;
  last_used_at?: string | null;
  spent_today_usd: number;
  spent_today?: number | null;
  spent_month_usd: number;
  spent_month?: number | null;
}

export interface MemberDetail {
  member: OrganizationMember;
  /** Absent from a gateway that does not send it yet, as the four lists below. */
  account?: MemberAccount;
  /** The member's spend for each of the last 30 days. */
  days: UsagePoint[];
  models: ModelUsage[];
  requests: OrganizationRequest[];
  projects?: MemberProject[];
  keys?: MemberKey[];
  /** The models the organization turned off for the member. */
  disabled_models?: string[];
  /** The models the platform administrator turned off: to read, not to change. */
  platform_disabled_models?: string[];
}

/**
 * The models of one member, as its organization sets them. `available` are
 * the models the platform lets the member use: the ones an organization can
 * turn off.
 */
export interface MemberModels {
  available: string[];
  disabled_models: string[];
  platform_disabled_models: string[];
}

export type AuditCategory = 'membership' | 'limits' | 'access' | 'organization';
export type AuditActor = 'platform_administrator' | 'member' | 'system';

export interface AuditEntry {
  id: number;
  timestamp: string;
  action: string;
  category: AuditCategory;
  /** A platform administrator is named without its account. */
  actor: AuditActor;
  actor_user_id?: string | null;
  actor_email?: string | null;
  target_type?: string | null;
  target_user_id?: string | null;
  target_email?: string | null;
  details?: Record<string, unknown> | null;
}

export interface AuditPage {
  total: number;
  entries: AuditEntry[];
}

export type MemberStatusFilter = 'active' | 'suspended' | 'removed' | 'pending_approval';
export type MemberSort =
  | 'name'
  | 'status'
  | 'last_request'
  | 'requests'
  | 'tokens'
  | 'spend'
  | 'today'
  | 'week'
  | 'month'
  | 'alltime'
  /** The share of the monthly limit that the spend of this month uses; no limit is last. */
  | 'limit_use'
  | 'joined';

/** How far into its monthly limit a member is: 80% of it or more, or all of it or more. */
export type MemberLimitState = 'near' | 'over';

export interface MemberQuery {
  /** The period of the requests and the tokens of a row; the spend columns do not follow it. */
  period?: OrganizationPeriod;
  search?: string;
  status?: MemberStatusFilter;
  limitState?: MemberLimitState;
  /**
   * The least spend of today and of this month, in `currency`. The gateway
   * refuses (409) a threshold in a currency that is not the ledger's.
   */
  minCostToday?: number;
  minCostMonth?: number;
  currency?: Currency;
  sortBy?: MemberSort;
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

export interface RequestQuery {
  period: OrganizationPeriod;
  search?: string;
  model?: string;
  status?: 'ok' | 'errors';
  limit?: number;
  /** The `next_before` of the page before. */
  before?: string;
}

export interface AuditQuery {
  /** Every row when left out. */
  period?: OrganizationPeriod;
  category?: AuditCategory;
  /** The rows that have this member as target or as actor. */
  member?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

/**
 * A monthly limit to save: the amount in the ledger currency, or `null` for
 * no limit. On a ledger that is not in USD the currency travels with it, so
 * the gateway refuses a page that was loaded before the ledger changed.
 */
export interface MonthlyLimit {
  amount: number | null;
  currency: Currency;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

async function read<T>(path: string): Promise<T> {
  return readJson<T>(await fetchWithAuth(API_BASE, `${ROOT}${path}`));
}

async function write<T>(method: 'POST' | 'PUT', path: string, body?: unknown): Promise<T> {
  const init: RequestInit =
    body === undefined
      ? { method }
      : {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        };
  return readJson<T>(await fetchWithAuth(API_BASE, `${ROOT}${path}`, init));
}

function limitBody(limit: MonthlyLimit): Record<string, unknown> {
  return limit.currency === 'USD'
    ? { monthly_limit_usd: limit.amount }
    : { monthly_limit: limit.amount, currency: limit.currency };
}

function member(userId: string): string {
  return `/members/${encodeURIComponent(userId)}`;
}

export function getOrganization(): Promise<Organization> {
  return read<Organization>('');
}

/**
 * The usage of a period. `granularity` chooses the points of the series; the
 * gateway has an hourly series for `1d` and `7d` only, and answers 400 for
 * one of `30d`.
 */
export function getOrganizationOverview(
  period: OrganizationPeriod,
  granularity?: 'hour' | 'day',
): Promise<OrganizationOverview> {
  return read<OrganizationOverview>(`/overview${query({ period, granularity })}`);
}

export function listOrganizationMembers(params: MemberQuery): Promise<MemberList> {
  return read<MemberList>(
    `/members${query({
      period: params.period,
      search: params.search,
      status: params.status,
      limit_state: params.limitState,
      min_cost_today: params.minCostToday,
      min_cost_month: params.minCostMonth,
      // Only with a threshold: the rest of the list does not depend on it.
      currency:
        params.minCostToday !== undefined || params.minCostMonth !== undefined
          ? params.currency
          : undefined,
      sort_by: params.sortBy,
      order: params.order,
      limit: params.limit,
      offset: params.offset,
    })}`,
  );
}

/** The two cards above the member list. */
export function getOrganizationMembersSummary(): Promise<MembersSummary> {
  return read<MembersSummary>('/members/summary');
}

export function getOrganizationMember(
  userId: string,
  period: OrganizationPeriod,
): Promise<MemberDetail> {
  return read<MemberDetail>(`${member(userId)}${query({ period })}`);
}

export function listOrganizationRequests(params: RequestQuery): Promise<RequestPage> {
  return read<RequestPage>(
    `/usage/requests${query({
      period: params.period,
      search: params.search,
      model: params.model,
      status: params.status,
      limit: params.limit,
      before: params.before,
    })}`,
  );
}

export async function listOrganizationModelUsage(
  period: OrganizationPeriod,
): Promise<ModelUsage[]> {
  const answer = await read<{ models: ModelUsage[] }>(`/usage/models${query({ period })}`);
  return answer.models;
}

/** The members with their usage in the period, the largest spend first. */
export function listOrganizationMemberUsage(params: {
  period: OrganizationPeriod;
  search?: string;
  limit?: number;
  offset?: number;
}): Promise<MemberList> {
  return read<MemberList>(
    `/usage/members${query({
      period: params.period,
      search: params.search,
      limit: params.limit,
      offset: params.offset,
    })}`,
  );
}

export function listOrganizationAudit(params: AuditQuery): Promise<AuditPage> {
  return read<AuditPage>(
    `/audit-log${query({
      period: params.period,
      category: params.category,
      member: params.member,
      search: params.search,
      limit: params.limit,
      offset: params.offset,
    })}`,
  );
}

/** Set a member's monthly limit, or clear it so the organization's default applies. */
export async function setMemberMonthlyLimit(userId: string, limit: MonthlyLimit): Promise<void> {
  await write<{ message: string }>('PUT', `${member(userId)}/monthly-limit`, limitBody(limit));
}

/** Set the default monthly limit of a member, or clear it. */
export function setDefaultMemberLimit(limit: MonthlyLimit): Promise<Organization> {
  return write<Organization>('PUT', '/default-member-limit', limitBody(limit));
}

/** The requests of the members in the last hour, day, week and month. */
export function getOrganizationRequestVolume(): Promise<RequestVolume> {
  return read<RequestVolume>('/usage/volume');
}

/** The models of one member: those it can use, and those that are off for it. */
export function getMemberModels(userId: string): Promise<MemberModels> {
  return read<MemberModels>(`${member(userId)}/models`);
}

/**
 * Replace the models the organization turns off for one member; an empty list
 * turns none off. The list of the platform administrator is not touched.
 */
export function setMemberModels(userId: string, disabledModels: string[]): Promise<MemberModels> {
  return write<MemberModels>('PUT', `${member(userId)}/models`, {
    disabled_models: disabledModels,
  });
}

/** Revoke one API key of a member. A revoked key stays revoked. */
export async function revokeMemberKey(userId: string, keyId: number): Promise<void> {
  await write<{ message: string }>(
    'POST',
    `${member(userId)}/keys/${encodeURIComponent(String(keyId))}/revoke`,
  );
}

/** Suspend a member's account: its sessions end and its API keys are revoked. */
export async function removeOrganizationMember(userId: string): Promise<void> {
  await write<{ message: string }>('POST', `${member(userId)}/remove`);
}

/** Make a member that the organization removed active again. */
export async function restoreOrganizationMember(userId: string): Promise<void> {
  await write<{ message: string }>('POST', `${member(userId)}/restore`);
}
