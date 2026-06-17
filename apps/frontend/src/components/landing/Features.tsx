interface Feature {
  title: string;
  body: string;
}

const FEATURES: Feature[] = [
  {
    title: 'Free to use',
    body: 'No credit card required. Generous quota for research and prototyping.',
  },
  {
    title: 'Drop-in OpenAI replacement',
    body: 'Point your existing OpenAI client at our base_url. No code changes required.',
  },
  {
    title: 'Frontier models',
    body: 'GLM, Minimax, Qwen, and Kimi models — all behind a single unified API.',
  },
  {
    title: 'Streaming and tool calls',
    body: 'Server-sent streaming, tool calls, and structured output supported end-to-end.',
  },
  {
    title: 'Live usage and keys',
    body: 'Track token usage, manage API keys, and monitor quotas from your dashboard.',
  },
];

export function Features(): JSX.Element {
  return (
    <section className="w-full py-16">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-serif text-3xl font-bold tracking-tight text-gray-900 sm:text-4xl">
          Why <span className="text-crimson">freeinference.org</span>
        </h2>
        <p className="mt-3 text-base text-gray-600">
          Everything you need to build and ship LLM-powered applications.
        </p>
      </div>

      <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature) => (
          <div
            key={feature.title}
            className="rounded-xl border border-gray-200 bg-white p-6 shadow-subtle transition-shadow duration-200 hover:shadow-card"
          >
            <h3 className="font-serif text-lg font-semibold text-gray-900">{feature.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-gray-600">{feature.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
