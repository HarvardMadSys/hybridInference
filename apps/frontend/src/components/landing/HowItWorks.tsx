interface Step {
  number: string;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    number: '1',
    title: 'Sign up',
    body: 'Create a free account with your email — no credit card needed.',
  },
  {
    number: '2',
    title: 'Create an API key',
    body: 'Generate a key from your dashboard in one click.',
  },
  {
    number: '3',
    title: 'Call the API',
    body: 'Use any OpenAI-compatible client. Just change the base URL.',
  },
];

export function HowItWorks(): JSX.Element {
  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <p className="text-sm font-semibold uppercase tracking-wider text-crimson">
          Getting started
        </p>
        <h2 className="mt-2 font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Get started in three steps
        </h2>
      </div>

      <div className="relative mt-12">
        <div
          aria-hidden
          className="absolute left-0 right-0 top-6 hidden h-px bg-gradient-to-r from-transparent via-crimson/30 to-transparent md:block"
        />
        <ol className="relative grid gap-10 md:grid-cols-3">
          {STEPS.map((step) => (
            <li key={step.number} className="flex flex-col items-center text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-full bg-crimson font-serif text-xl font-bold text-white shadow-card ring-4 ring-white">
                {step.number}
              </span>
              <h3 className="mt-5 font-serif text-xl font-semibold text-gray-900">{step.title}</h3>
              <p className="mt-2 max-w-xs text-sm leading-relaxed text-gray-600">{step.body}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
