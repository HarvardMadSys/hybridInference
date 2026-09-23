import type { Metadata } from 'next';
import { publicPageMetadata } from '@/site-ui/meta';
import { TermsPageContent } from '@/site-ui/SiteUiBoundary';

// The tab title is the page's too: the module's `meta.terms.*` wording when it
// has some, the console's "Terms of Service" otherwise.
export function generateMetadata(): Promise<Metadata> {
  return publicPageMetadata('terms');
}

export default function TermsPage(): JSX.Element {
  // The *frame* is the Site UI's business: the module's `TermsFrame` when it
  // supplies one, the console's own card otherwise. Chosen in
  // `TermsPageContent`, which is also what decides whether the console
  // container wraps this page — so the legal text and its slots are identical
  // either way, and this page names no layout at all.
  return <TermsPageContent />;
}
