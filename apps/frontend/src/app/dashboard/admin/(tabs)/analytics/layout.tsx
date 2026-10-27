import { AnalyticsSectionNav } from '../../AnalyticsSectionNav';

/** Overview, Token usage and Request origins share the Analytics section control. */
export default function AnalyticsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <AnalyticsSectionNav />
      {children}
    </div>
  );
}
