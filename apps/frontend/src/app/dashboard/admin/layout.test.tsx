// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createPortal } from 'react-dom';
import { LocaleProvider, useLocale } from '@/components/providers/LocaleProvider';
import { useT } from '@/components/providers/useT';
import { ConsoleCopyProvider } from '@/site-ui/appearance';
import catalog from '@/lib/i18n/messages/zh-CN.json';
import AdminLayout from './layout';
import { RouteDeploymentField } from './RouteDeploymentField';

vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user: { is_admin: true } } }),
}));
vi.mock('@/components/features/admin/AdminTabNav', () => ({ AdminTabNav: () => null }));
afterEach(cleanup);

function LanguageProbe({ portal = false }: { portal?: boolean }) {
  const t = useT();
  const content = (
    <span data-testid={portal ? 'portal' : 'user'}>
      {useLocale()}:{t('admin.routing.deployment_type', 'Deployment type')}
    </span>
  );
  return portal ? createPortal(content, document.body) : content;
}

it('keeps admin fields and portals English while the surrounding user console stays Chinese', () => {
  render(
    <LocaleProvider locale="zh-CN" catalog={catalog} moduleLocale="">
      <ConsoleCopyProvider value={true}>
        <LanguageProbe />
        <AdminLayout>
          <RouteDeploymentField id="deployment" value="auto" onChange={() => {}} />
          <LanguageProbe portal />
        </AdminLayout>
      </ConsoleCopyProvider>
    </LocaleProvider>,
  );
  expect(screen.getByTestId('user')).toHaveTextContent('zh-CN:部署类型');
  expect(screen.getByTestId('portal')).toHaveTextContent('en:Deployment type');
  expect(screen.getByLabelText('Deployment type')).toHaveTextContent(
    'Auto (use endpoint hostname)',
  );
  fireEvent.click(screen.getByLabelText('Deployment type'));
  expect(screen.getByRole('option', { name: 'Self-hosted engine' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Provider API' })).toBeInTheDocument();
});
