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

export interface MemberDetail {
  member: OrganizationMember;
  /** The member's spend for each of the last 30 days. */
  days: UsagePoint[];
  models: ModelUsage[];
  requests: OrganizationRequest[];
}

export type AuditCategory = 'membership' | 'limits' | 'organization';
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
  | 'month'
  /** The share of the monthly limit that the spend of this month uses; no limit is last. */
  | 'limit_use'
  | 'joined';

export interface MemberQuery {
  period: OrganizationPeriod;
  search?: string;
  status?: MemberStatusFilter;
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

export function getOrganizationOverview(period: OrganizationPeriod): Promise<OrganizationOverview> {
  return read<OrganizationOverview>(`/overview${query({ period })}`);
}

export function listOrganizationMembers(params: MemberQuery): Promise<MemberList> {
  return read<MemberList>(
    `/members${query({
      period: params.period,
      search: params.search,
      status: params.status,
      sort_by: params.sortBy,
      order: params.order,
      limit: params.limit,
      offset: params.offset,
    })}`,
  );
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

/** Suspend a member's account: its sessions end and its API keys are revoked. */
export async function removeOrganizationMember(userId: string): Promise<void> {
  await write<{ message: string }>('POST', `${member(userId)}/remove`);
}

/** Make a member that the organization removed active again. */
export async function restoreOrganizationMember(userId: string): Promise<void> {
  await write<{ message: string }>('POST', `${member(userId)}/restore`);
}
