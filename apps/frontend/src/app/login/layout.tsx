import type { Metadata } from 'next';
import { publicPageMetadata } from '@/site-ui/meta';

// The page is a client component, and a client component cannot export
// `generateMetadata`. This segment gives it a title and description: the
// module's `meta.login.*` wording when it has some, the site's own otherwise.
export function generateMetadata(): Promise<Metadata> {
  return publicPageMetadata('login');
}

export default function LoginLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
