import { config } from '@/config/env';
import { StatsContent } from './StatsContent';

export const metadata = {
  title: `Usage stats | ${config.appName}`,
  description: `Tokens served, countries, languages and agents on ${config.appName}, refreshed daily.`,
};

export default function StatsPage(): JSX.Element {
  return <StatsContent />;
}
