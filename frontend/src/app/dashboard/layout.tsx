/**
 * Dashboard layout: overrides root layout's vertical centering so that
 * content-heavy pages (admin panel, settings, etc.) align to the top.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <div className="w-full self-start">{children}</div>;
}
