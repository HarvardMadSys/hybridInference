import { ConfigurationTab } from '@/components/features/admin/ConfigurationTab';

interface PageProps {
  searchParams?: Promise<{ missing?: string }>;
}

// `?missing=1` is where the "required settings are missing" banner links: the
// tab opens filtered to what needs a value.
export default async function Page({ searchParams }: PageProps) {
  const params = await searchParams;
  return <ConfigurationTab initialMissingOnly={params?.missing === '1'} />;
}
