import { ProviderSelectionProvider } from '@/components/features/admin/providerSelection';

import { ProvidersSectionNav } from '../../ProvidersSectionNav';

/**
 * Every Providers page shares the section control, and the provider Keys
 * shows: the layout stays mounted between its pages, so a provider chosen on
 * Overview is still chosen on the way back from Performance.
 */
export default function ProvidersLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <ProvidersSectionNav />
      <ProviderSelectionProvider>{children}</ProviderSelectionProvider>
    </div>
  );
}
