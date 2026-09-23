import type { Metadata } from 'next';
import { publicPageMetadata } from '@/site-ui/meta';
import { HomeContent } from './HomeContent';

// A server component for the metadata alone: the page itself is client-side,
// and a client component cannot export `generateMetadata`.
export function generateMetadata(): Promise<Metadata> {
  return publicPageMetadata('home');
}

export default function HomePage(): JSX.Element {
  return <HomeContent />;
}
