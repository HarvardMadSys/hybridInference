import { redirect } from 'next/navigation';

/** Projects are managed on the API Keys page now; keep old links working. */
export default function ProjectsPage(): never {
  redirect('/dashboard/keys');
}
