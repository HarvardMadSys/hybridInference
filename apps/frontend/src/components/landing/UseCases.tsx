interface UseCaseApp {
  name: string;
  href?: string;
}

interface UseCase {
  title: string;
  body: string;
  apps: UseCaseApp[];
  icon: JSX.Element;
}

const USE_CASES: UseCase[] = [
  {
    title: 'Coding agents',
    body: 'Power autonomous and assisted coding workflows with frontier models via OpenAI- or Anthropic-compatible endpoints.',
    apps: [
      { name: 'Claude Code', href: 'https://claude.com/claude-code' },
      { name: 'Kilo', href: 'https://kilocode.ai' },
    ],
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
          d="m6.75 7.5 3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0 0 21 18V6a2.25 2.25 0 0 0-2.25-2.25H5.25A2.25 2.25 0 0 0 3 6v12a2.25 2.25 0 0 0 2.25 2.25Z"
        />
      </svg>
    ),
  },
  {
    title: 'Personal assistants',
    body: 'Build conversational assistants and agents that plan, reason, and act on your behalf.',
    apps: [{ name: 'Hermes' }, { name: 'OpenClaw' }],
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
          d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 0 1 .865-.501 48.172 48.172 0 0 0 3.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0 0 12 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018Z"
        />
      </svg>
    ),
  },
];

export function UseCases(): JSX.Element {
  return (
    <section className="w-full py-16" aria-label="use cases">
      <div className="mx-auto max-w-3xl text-center">
        <p className="text-sm font-semibold uppercase tracking-wider text-crimson">Use cases</p>
        <h2 className="mt-2 font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Supported use cases
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Drop us in wherever you need an OpenAI- or Anthropic-compatible endpoint.
        </p>
      </div>

      <div className="mx-auto mt-12 grid max-w-4xl gap-6 sm:grid-cols-2">
        {USE_CASES.map((useCase) => (
          <div
            key={useCase.title}
            className="group relative overflow-hidden rounded-2xl border border-gray-200 bg-white p-6 shadow-subtle transition-all duration-200 hover:-translate-y-1 hover:border-crimson/30 hover:shadow-card"
          >
            <span className="inline-flex h-12 w-12 items-center justify-center rounded-xl bg-crimson/10 text-crimson transition-colors duration-200 group-hover:bg-crimson group-hover:text-white">
              {useCase.icon}
            </span>
            <h3 className="mt-5 font-serif text-lg font-semibold text-gray-900">{useCase.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-gray-600">{useCase.body}</p>
            <ul className="mt-4 flex flex-wrap gap-2">
              {useCase.apps.map((app) => (
                <li key={app.name}>
                  {app.href ? (
                    <a
                      href={app.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs font-medium text-gray-700 transition-colors duration-200 hover:border-crimson hover:bg-crimson/5 hover:text-crimson"
                    >
                      {app.name}
                      <span className="sr-only"> (opens in a new tab)</span>
                    </a>
                  ) : (
                    <span className="inline-flex items-center rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs font-medium text-gray-700">
                      {app.name}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
