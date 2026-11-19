// Shown while a tab's chunk downloads on first visit: the tab links do not
// prefetch, so a switch on a cold cache would otherwise sit on the old page.
export default function Loading() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="rounded-lg border border-gray-200 bg-white p-4">
          <div className="mb-3 h-2.5 w-24 animate-pulse rounded bg-gray-200" />
          <div className="h-7 w-20 animate-pulse rounded bg-gray-200" />
        </div>
      ))}
    </div>
  );
}
