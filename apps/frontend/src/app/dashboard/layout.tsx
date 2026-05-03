/**
 * Dashboard layout: lets dashboard pages occupy the parent <main>'s full
 * max-w-5xl width without the previous w-screen full-bleed hack.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <div className="w-full">{children}</div>;
}
