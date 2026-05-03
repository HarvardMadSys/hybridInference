/**
 * Dashboard layout: aligns content to the top of the flex column instead of
 * inheriting the root layout's vertical centering.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <div className="w-full self-start">{children}</div>;
}
