const sponsors = [
  {
    name: 'NVIDIA',
    alt: 'NVIDIA logo',
    src: '/sponsors/nvidia.svg',
    className: 'h-10 w-auto',
  },
  {
    name: 'Harvard SEAS',
    alt: 'Harvard SEAS logo',
    src: '/sponsors/harvard-seas.svg',
    className: 'h-12 w-auto',
  },
];

export function Sponsors(): JSX.Element {
  return (
    <section className="px-4 py-6 sm:px-6 lg:px-8" aria-label="sponsors">
      <div className="mx-auto w-full max-w-4xl rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
        <p className="text-center text-sm font-semibold uppercase tracking-wide text-gray-500">
          Sponsors
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-8 sm:gap-10">
          {sponsors.map((sponsor) => (
            <img
              key={sponsor.name}
              src={sponsor.src}
              alt={sponsor.alt}
              className={sponsor.className}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
