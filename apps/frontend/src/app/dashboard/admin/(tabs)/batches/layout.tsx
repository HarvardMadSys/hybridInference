import { BatchesSectionNav } from '../../BatchesSectionNav';

/** The batch list, the batch requests and the model queue share one section control. */
export default function BatchesLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <BatchesSectionNav />
      {children}
    </div>
  );
}
