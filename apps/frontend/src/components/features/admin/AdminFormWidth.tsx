/**
 * Narrows a form- or feed-style admin tab inside the wide admin frame.
 *
 * The admin chrome is wide so data tables can show every column, but long
 * text inputs, toggle rows and a one-column event feed get harder to read when
 * stretched to 1440px: labels drift away from their controls. Those tabs wrap
 * their content in this, which keeps them at the width they were designed for
 * and left-aligned with the tab bar above.
 */
export function AdminFormWidth({ children }: { children: React.ReactNode }) {
  return <div className="max-w-4xl">{children}</div>;
}
