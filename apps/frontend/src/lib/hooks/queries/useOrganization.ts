import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getMemberModels,
  getOrganization,
  getOrganizationMember,
  getOrganizationMembersSummary,
  getOrganizationOverview,
  getOrganizationRequestVolume,
  listOrganizationAudit,
  listOrganizationMemberUsage,
  listOrganizationMembers,
  listOrganizationModelUsage,
  listOrganizationRequests,
  removeOrganizationMember,
  restoreOrganizationMember,
  revokeMemberKey,
  setDefaultMemberLimit,
  setMemberModels,
  setMemberMonthlyLimit,
  type AuditQuery,
  type MemberQuery,
  type MonthlyLimit,
  type OrganizationPeriod,
  type RequestQuery,
} from '@/lib/api/organization';
import { useMoney } from '@/lib/hooks/useMoney';

const THIRTY_S = 30 * 1000;

/**
 * Every cached answer of the organization console. A write invalidates the
 * whole prefix: a limit or a removal changes the member list, the member's
 * details, the organization's totals and the audit log together.
 */
export const ORGANIZATION_QUERY_KEY = ['organization'] as const;

/** The key of one read; the ledger currency is part of it, as amounts are read in it. */
function useKey(...parts: unknown[]): unknown[] {
  const { currency } = useMoney();
  return [...ORGANIZATION_QUERY_KEY, ...parts, currency];
}

export function useOrganization(enabled = true) {
  return useQuery({
    queryKey: useKey('record'),
    queryFn: getOrganization,
    staleTime: THIRTY_S,
    enabled,
  });
}

export function useOrganizationOverview(period: OrganizationPeriod, granularity?: 'hour' | 'day') {
  return useQuery({
    queryKey: useKey('overview', period, granularity ?? null),
    queryFn: () => getOrganizationOverview(period, granularity),
    staleTime: THIRTY_S,
    placeholderData: keepPreviousData,
  });
}

export function useOrganizationMembers(params: MemberQuery) {
  return useQuery({
    queryKey: useKey('members', params),
    queryFn: () => listOrganizationMembers(params),
    staleTime: THIRTY_S,
    // A new page, search or order keeps the rows on screen until its answer arrives.
    placeholderData: keepPreviousData,
  });
}

export function useOrganizationMembersSummary() {
  return useQuery({
    queryKey: useKey('members-summary'),
    queryFn: getOrganizationMembersSummary,
    staleTime: THIRTY_S,
  });
}

/** The models of one member; asked for when its dialog opens. */
export function useOrganizationMemberModels(userId: string | null) {
  return useQuery({
    queryKey: useKey('member-models', userId),
    queryFn: () => getMemberModels(userId as string),
    staleTime: THIRTY_S,
    enabled: userId !== null,
  });
}

export function useOrganizationRequestVolume() {
  return useQuery({
    queryKey: useKey('volume'),
    queryFn: getOrganizationRequestVolume,
    staleTime: THIRTY_S,
  });
}

export function useOrganizationMember(userId: string | null, period: OrganizationPeriod) {
  return useQuery({
    queryKey: useKey('member', userId, period),
    queryFn: () => getOrganizationMember(userId as string, period),
    staleTime: THIRTY_S,
    enabled: userId !== null,
  });
}

export function useOrganizationRequests(params: RequestQuery) {
  return useQuery({
    queryKey: useKey('requests', params),
    queryFn: () => listOrganizationRequests(params),
    staleTime: THIRTY_S,
    placeholderData: keepPreviousData,
  });
}

export function useOrganizationModelUsage(period: OrganizationPeriod, enabled = true) {
  return useQuery({
    queryKey: useKey('models', period),
    queryFn: () => listOrganizationModelUsage(period),
    staleTime: THIRTY_S,
    enabled,
    placeholderData: keepPreviousData,
  });
}

export function useOrganizationMemberUsage(
  params: { period: OrganizationPeriod; search?: string; limit?: number; offset?: number },
  enabled = true,
) {
  return useQuery({
    queryKey: useKey('member-usage', params),
    queryFn: () => listOrganizationMemberUsage(params),
    staleTime: THIRTY_S,
    enabled,
    placeholderData: keepPreviousData,
  });
}

export function useOrganizationAudit(params: AuditQuery) {
  return useQuery({
    queryKey: useKey('audit', params),
    queryFn: () => listOrganizationAudit(params),
    staleTime: THIRTY_S,
    placeholderData: keepPreviousData,
  });
}

function useOrganizationWrite<V>(write: (variables: V) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ORGANIZATION_QUERY_KEY }),
  });
}

export function useSetMemberMonthlyLimit() {
  return useOrganizationWrite(({ userId, limit }: { userId: string; limit: MonthlyLimit }) =>
    setMemberMonthlyLimit(userId, limit),
  );
}

export function useSetDefaultMemberLimit() {
  return useOrganizationWrite((limit: MonthlyLimit) => setDefaultMemberLimit(limit));
}

export function useSetMemberModels() {
  return useOrganizationWrite(
    ({ userId, disabledModels }: { userId: string; disabledModels: string[] }) =>
      setMemberModels(userId, disabledModels),
  );
}

export function useRevokeMemberKey() {
  return useOrganizationWrite(({ userId, keyId }: { userId: string; keyId: number }) =>
    revokeMemberKey(userId, keyId),
  );
}

export function useRemoveOrganizationMember() {
  return useOrganizationWrite((userId: string) => removeOrganizationMember(userId));
}

export function useRestoreOrganizationMember() {
  return useOrganizationWrite((userId: string) => restoreOrganizationMember(userId));
}
