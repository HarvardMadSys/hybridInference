import { BatchesSectionNav } from '../../BatchesSectionNav';

/** The batch list and the batch requests share one section control. */
export default function BatchesLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <BatchesSectionNav />
      {children}
    </div>
  );
}
