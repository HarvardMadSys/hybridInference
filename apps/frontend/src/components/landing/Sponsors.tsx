'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';

export function Sponsors(): JSX.Element | null {
  const { sponsors } = useBranding();
  if (sponsors.length === 0) {
    return null;
  }
  return (
    <section className="px-4 py-6 sm:px-6 lg:px-8" aria-label="sponsors">
      <div className="mx-auto w-full max-w-4xl rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
        <p className="text-center text-sm font-semibold uppercase tracking-wide text-gray-500">
          Sponsors
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-x-10 gap-y-6 sm:gap-x-14">
          {sponsors.map((sponsor) => (
            // Runtime assets cannot use next/image because the neutral image
            // has no build-time knowledge of their origin.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={sponsor.name}
              src={sponsor.src}
              alt={sponsor.alt}
              width={sponsor.width}
              height={sponsor.height}
              className={`${sponsor.className} w-auto`}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
