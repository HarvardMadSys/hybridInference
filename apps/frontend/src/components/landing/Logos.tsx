export function Logos(): JSX.Element {
  return (
    <section className="px-4 py-6 sm:px-6 lg:px-8" aria-label="logos">
      <div className="mx-auto w-full max-w-4xl rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
        <p className="text-center text-sm font-semibold uppercase tracking-wide text-gray-500">
          Logos
        </p>
        <div className="mt-4 flex flex-wrap items-center justify-center gap-3">
          <span className="rounded-full bg-gray-100 px-4 py-2 text-sm font-medium text-gray-800">
            NVIDIA
          </span>
          <span className="rounded-full bg-gray-100 px-4 py-2 text-sm font-medium text-gray-800">
            Harvard SEAS
          </span>
        </div>
      </div>
    </section>
  );
}
