/**
 * Dashboard layout: a column as tall as the room the console chrome gives it,
 * so a page that fills the window (Chat) can, and every other page simply
 * takes the width of `main`.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <div className="flex min-h-0 w-full flex-1 flex-col">{children}</div>;
}
