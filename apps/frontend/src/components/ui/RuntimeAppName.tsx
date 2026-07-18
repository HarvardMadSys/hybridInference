'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';

export function RuntimeAppName(): JSX.Element {
  return <>{useBranding().appName}</>;
}
