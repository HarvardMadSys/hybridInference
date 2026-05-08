import { CodeExample, Features, Hero, HowItWorks } from '@/components/landing';

export default function HomePage(): JSX.Element {
  return (
    <div className="flex w-full flex-col gap-4">
      <Hero />
      <Features />
      <HowItWorks />
      <CodeExample />
      <footer className="px-4 pb-6 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
        <p>Service is provided without guarantee.</p>
        <p className="mt-1">All prompts and responses are logged.</p>
      </footer>
    </div>
  );
}
