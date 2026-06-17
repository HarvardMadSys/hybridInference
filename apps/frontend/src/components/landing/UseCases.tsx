interface UseCaseApp {
  name: string;
  href?: string;
}

interface UseCase {
  title: string;
  body: string;
  apps: UseCaseApp[];
}

const USE_CASES: UseCase[] = [
  {
    title: 'Coding agents',
    body: 'Power autonomous and assisted coding workflows with frontier models behind an OpenAI-compatible API.',
    apps: [
      { name: 'Claude Code', href: 'https://claude.com/claude-code' },
      { name: 'Kilo', href: 'https://kilocode.ai' },
    ],
  },
  {
    title: 'Personal assistants',
    body: 'Build conversational assistants and agents that plan, reason, and act on your behalf.',
    apps: [{ name: 'Hermes' }, { name: 'OpenClaw' }],
  },
];

export function UseCases(): JSX.Element {
  return (
    <section className="w-full py-16" aria-label="use cases">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Supported use cases
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Drop us in wherever you need an OpenAI-compatible endpoint.
        </p>
      </div>

      <div className="mx-auto mt-10 grid max-w-4xl gap-6 sm:grid-cols-2">
        {USE_CASES.map((useCase) => (
          <div
            key={useCase.title}
            className="rounded-xl border border-gray-200 bg-white p-6 shadow-subtle transition-shadow duration-200 hover:shadow-card"
          >
            <h3 className="font-serif text-lg font-semibold text-gray-900">{useCase.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-gray-600">{useCase.body}</p>
            <ul className="mt-4 flex flex-wrap gap-2">
              {useCase.apps.map((app) => (
                <li key={app.name}>
                  {app.href ? (
                    <a
                      href={app.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs font-medium text-gray-700 transition-colors duration-200 hover:border-crimson hover:text-crimson"
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
