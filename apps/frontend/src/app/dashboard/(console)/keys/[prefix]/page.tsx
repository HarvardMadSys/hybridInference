'use client';

import { useParams } from 'next/navigation';
import { ApiKeyDetail } from '@/components/features/dashboard/ApiKeyDetail';

export default function KeyDetailPage() {
  const params = useParams<{ prefix: string }>();
  return <ApiKeyDetail keyPrefix={decodeURIComponent(params.prefix)} />;
}
