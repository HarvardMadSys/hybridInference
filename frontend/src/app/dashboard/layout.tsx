/**
 * Dashboard layout: overrides root layout's vertical centering so that
 * content-heavy pages (admin panel, settings, etc.) align to the top.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative left-1/2 w-screen -translate-x-1/2 self-start px-6">{children}</div>
  );
}
