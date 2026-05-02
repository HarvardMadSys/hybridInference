import Link from 'next/link';

export function Hero(): JSX.Element {
  return (
    <section className="relative w-full overflow-hidden rounded-2xl bg-gradient-to-br from-white via-gray-50 to-red-50/30 px-6 py-20 text-center shadow-subtle">
      <h1 className="mx-auto max-w-3xl font-serif text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl md:text-6xl">
        Free LLM Inference <span className="text-crimson">for Research</span>
      </h1>
      <p className="mx-auto mt-6 max-w-2xl text-base text-gray-600 sm:text-lg">
        OpenAI-compatible API powered by frontier open models. Built at{' '}
        <a
          href="https://madsys.seas.harvard.edu"
          className="text-crimson hover:underline"
          target="_blank"
          rel="noreferrer"
        >
          Harvard SEAS
        </a>
        .
      </p>
      <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
        <Link
          href="/signup"
          className="inline-flex h-11 items-center justify-center rounded-md bg-crimson px-6 text-base font-medium text-white shadow-sm transition-colors duration-200 hover:bg-crimson-dark focus:outline-none focus:ring-2 focus:ring-crimson focus:ring-offset-2"
        >
          Sign up free
        </Link>
        <Link
          href="/login"
          className="inline-flex h-11 items-center justify-center rounded-md border border-gray-300 bg-white px-6 text-base font-medium text-gray-900 shadow-sm transition-colors duration-200 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2"
        >
          Sign in
        </Link>
      </div>
    </section>
  );
}
