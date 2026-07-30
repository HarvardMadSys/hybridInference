// Shown while a tab's chunk downloads on first visit — tab links no longer
// prefetch, so switches on cold cache would otherwise sit on the old page.
export default function Loading() {
  return (
    <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="rounded-xl border border-gray-100 bg-gray-50 p-5">
          <div className="mb-3 h-2.5 w-24 animate-pulse rounded bg-gray-200" />
          <div className="h-8 w-16 animate-pulse rounded bg-gray-200" />
          <div className="mt-4 h-10 w-full animate-pulse rounded bg-gray-200" />
        </div>
      ))}
    </div>
  );
}
