import { GeoGlobe } from '@/components/features/admin/geo/GeoGlobe';

export default function GeoAnalyticsPage() {
  return (
    <div>
      <h2 className="text-xl font-semibold tracking-tight text-gray-900">Request origins</h2>
      <p className="mt-1 text-sm text-gray-500">
        See where requests come from and how traffic changes over time.
      </p>
      <GeoGlobe />
    </div>
  );
}
