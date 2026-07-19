import { config } from '@/config/env';
import { TermsContent } from './TermsContent';

export const metadata = {
  title: `Terms of Service | ${config.appName}`,
  description: `Terms of Service for ${config.appName}.`,
};

export default function TermsPage(): JSX.Element {
  return <TermsContent />;
}
