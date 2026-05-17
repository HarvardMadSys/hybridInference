import { CodeExample, Features, Hero, HowItWorks, Sponsors } from '@/components/landing';

export default function HomePage(): JSX.Element {
  return (
    <div className="flex w-full flex-col gap-4">
      <Hero />
      <Features />
      <HowItWorks />
      <CodeExample />
      <Sponsors />
      <footer className="px-4 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
        <p>Service is provided without guarantee.</p>
        <p className="mt-1">All prompts and responses are logged for research purposes.</p>
      </footer>
      <div className="mx-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-center text-sm text-amber-800 sm:mx-6 lg:mx-8">
        All prompts and responses are logged.
      </div>
    </div>
  );
}
