import { RoutingSectionNav } from '../../RoutingSectionNav';

/** The routes and the model catalog share one section control. */
export default function RoutingLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <RoutingSectionNav />
      {children}
    </div>
  );
}
