import { branding } from '@/config/branding';
interface Feature {
  title: string;
  body: string;
  icon: JSX.Element;
}

const FEATURES: Feature[] = [
  {
    title: 'Free to use',
    body: 'No credit card required. Generous quota for research and prototyping.',
    icon: (
      <svg
        className="h-6 w-6"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        aria-hidden
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456Z"
        />
      </svg>
    ),
  },
  {
    title: 'Drop-in OpenAI replacement',
    body: 'Point your existing OpenAI client at our base_url. No code changes required.',
    icon: (
      <svg
        className="h-6 w-6"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        aria-hidden
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M7.5 21 3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5"
        />
      </svg>
    ),
  },
  {
    title: 'Frontier models',
    body: 'GLM, Minimax, Qwen, and Kimi models — all behind a single unified API.',
    icon: (
      <svg
        className="h-6 w-6"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        aria-hidden
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M8.25 3v1.5M4.5 8.25H3m18 0h-1.5M4.5 12H3m18 0h-1.5m-15 3.75H3m18 0h-1.5M8.25 19.5V21M12 3v1.5m0 15V21m3.75-18v1.5m0 15V21m-9-1.5h10.5a2.25 2.25 0 0 0 2.25-2.25V6.75a2.25 2.25 0 0 0-2.25-2.25H6.75A2.25 2.25 0 0 0 4.5 6.75v10.5a2.25 2.25 0 0 0 2.25 2.25Zm.75-12h9v9h-9v-9Z"
        />
      </svg>
    ),
  },
];

export function Features(): JSX.Element {
  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <p className="text-sm font-semibold uppercase tracking-wider text-crimson">Features</p>
        <h2 className="mt-2 font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Why <span className="text-crimson">{branding.siteHost}</span>
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Everything you need to build and ship LLM-powered applications.
        </p>
      </div>

      <div className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature) => (
          <div
            key={feature.title}
            className="group relative overflow-hidden rounded-2xl border border-gray-200 bg-white p-6 shadow-subtle transition-all duration-200 hover:-translate-y-1 hover:border-crimson/30 hover:shadow-card"
          >
            <span className="inline-flex h-12 w-12 items-center justify-center rounded-xl bg-crimson/10 text-crimson transition-colors duration-200 group-hover:bg-crimson group-hover:text-white">
              {feature.icon}
            </span>
            <h3 className="mt-5 font-serif text-lg font-semibold text-gray-900">{feature.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-gray-600">{feature.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
