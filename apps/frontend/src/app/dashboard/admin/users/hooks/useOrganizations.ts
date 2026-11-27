'use client';

import { useQuery } from '@tanstack/react-query';
import { listAdminOrganizations, type AdminOrganization } from '@/lib/api/admin';

const FIVE_MIN = 5 * 60 * 1000;
const NONE: AdminOrganization[] = [];

/**
 * The organizations of the deployment, for the filter and the column of the
 * Users tab. None on a deployment that has no organization, and none when the
 * read fails: the tab then looks as it does without organizations.
 */
export function useOrganizations(): AdminOrganization[] {
  const { data } = useQuery({
    queryKey: ['admin', 'organizations', 'names'],
    queryFn: listAdminOrganizations,
    staleTime: FIVE_MIN,
    retry: false,
  });
  return data ?? NONE;
}
