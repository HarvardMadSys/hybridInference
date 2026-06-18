import {
  CodeExample,
  Features,
  Hero,
  HowItWorks,
  Sponsors,
  Updates,
  UpdatesBanner,
  UseCases,
} from '@/components/landing';

export default function HomePage(): JSX.Element {
  return (
    <div className="flex w-full flex-col gap-4">
      <UpdatesBanner />
      <Hero />
      <Updates />
      <Features />
      <UseCases />
      <HowItWorks />
      <CodeExample />
      <Sponsors />
      <footer className="mt-16 border-t border-gray-200 px-4 py-8 text-center text-xs text-gray-500 sm:px-6 lg:px-8">
        <p>Service is provided without guarantee.</p>
        <p className="mt-1">All prompts and responses are logged for research purposes.</p>
      </footer>
    </div>
  );
}
