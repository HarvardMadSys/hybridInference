import Link from 'next/link';
import { GeoGlobe } from '@/components/features/admin/geo/GeoGlobe';

export default function GeoAnalyticsPage() {
  return (
    <div className="mt-6">
      <Link
        className="text-xs font-medium text-gray-500 hover:text-gray-900"
        href="/dashboard/admin/analytics"
      >
        ← Analytics
      </Link>
      <h2 className="mt-3 text-xl font-semibold tracking-tight text-gray-900">Request origins</h2>
      <p className="mt-1 text-sm text-gray-500">
        See where FreeInference requests come from and how traffic changes over time.
      </p>
      <GeoGlobe />
    </div>
  );
}
