import type { Metadata } from 'next';
import { loadRuntimeSiteConfig } from '@/config/site-config.server';
import { pageMetadata } from '@/config/site-metadata';

// The page is a client component, which cannot export `generateMetadata`; this
// segment names it. A console page, so the wording is the console's own.
export async function generateMetadata(): Promise<Metadata> {
  return pageMetadata(
    await loadRuntimeSiteConfig(),
    'Set up',
    'Create the first administrator and configure this deployment.',
  );
}

export default function SetupLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
