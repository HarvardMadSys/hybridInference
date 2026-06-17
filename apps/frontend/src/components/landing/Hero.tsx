import Link from 'next/link';

const TRUST_POINTS = [
  'No credit card required',
  'OpenAI & Anthropic compatible',
  'Frontier open models',
];

export function Hero(): JSX.Element {
  return (
    <section className="relative w-full overflow-hidden rounded-3xl border border-gray-100 bg-gradient-to-br from-white via-gray-50 to-red-50/40 px-6 py-24 text-center shadow-subtle">
      {/* Decorative background accents */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="animate-float-slow absolute -left-24 -top-24 h-72 w-72 transform-gpu rounded-full bg-crimson/10 blur-3xl" />
        <div
          className="animate-float-slow absolute -bottom-32 -right-16 h-80 w-80 transform-gpu rounded-full bg-red-200/40 blur-3xl"
          style={{ animationDelay: '-7s' }}
        />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(165,28,48,0.06),_transparent_55%)]" />
      </div>

      <div className="animate-fade-in-up relative">
        <a
          href="https://madsys.seas.harvard.edu"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 rounded-full border border-crimson/20 bg-white/70 px-4 py-1.5 text-xs font-medium text-crimson shadow-subtle backdrop-blur transition-colors duration-200 hover:bg-white"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-crimson" />
          Built at Harvard SEAS · MadSys Lab
        </a>

        <h1 className="mx-auto mt-6 max-w-3xl font-serif text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl md:text-6xl">
          FreeInference{' '}
          <span className="bg-gradient-to-r from-crimson via-crimson-light to-crimson bg-clip-text text-transparent">
            for open-source, research and education
          </span>
        </h1>

        <p className="mx-auto mt-6 max-w-2xl text-base text-gray-600 sm:text-lg">
          An OpenAI-compatible API powered by frontier open models — free for the research
          community.
        </p>

        <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
          <Link
            href="/signup"
            className="group inline-flex h-12 items-center justify-center gap-2 rounded-xl bg-crimson px-7 text-base font-medium text-white shadow-card transition-all duration-200 hover:-translate-y-0.5 hover:bg-crimson-dark hover:shadow-lg focus:outline-none focus:ring-2 focus:ring-crimson focus:ring-offset-2"
          >
            Sign up free
            <svg
              className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden
            >
              <path
                fillRule="evenodd"
                d="M3 10a.75.75 0 0 1 .75-.75h8.69L9.22 6.03a.75.75 0 1 1 1.06-1.06l4.5 4.5a.75.75 0 0 1 0 1.06l-4.5 4.5a.75.75 0 1 1-1.06-1.06l3.22-3.22H3.75A.75.75 0 0 1 3 10Z"
                clipRule="evenodd"
              />
            </svg>
          </Link>
          <Link
            href="/login"
            className="inline-flex h-12 items-center justify-center rounded-xl border border-gray-300 bg-white/80 px-7 text-base font-medium text-gray-900 shadow-subtle backdrop-blur transition-all duration-200 hover:-translate-y-0.5 hover:bg-white hover:shadow-card focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
          >
            Sign in
          </Link>
        </div>

        <ul className="mt-9 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm text-gray-500">
          {TRUST_POINTS.map((point) => (
            <li key={point} className="inline-flex items-center gap-1.5">
              <svg
                className="h-4 w-4 text-crimson"
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden
              >
                <path
                  fillRule="evenodd"
                  d="M16.704 5.29a1 1 0 0 1 .006 1.414l-7.25 7.36a1 1 0 0 1-1.424.006l-3.75-3.75a1 1 0 1 1 1.414-1.414l3.038 3.038 6.546-6.648a1 1 0 0 1 1.414-.006Z"
                  clipRule="evenodd"
                />
              </svg>
              {point}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
