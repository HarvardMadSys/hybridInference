import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import type { Metadata } from 'next';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { buildTimeSiteConfig } from '@/config/site-config';
import { META_MESSAGE_KEYS } from './contract';

/**
 * Page titles and descriptions: the module's server `metaMessages` word the
 * public routes, and nothing else.
 */

vi.mock('@/config/site-config.server', () => ({
  loadRuntimeSiteConfig: async () => ({
    ...buildTimeSiteConfig,
    branding: { ...buildTimeSiteConfig.branding, appName: 'Runtime Console' },
  }),
}));

type GenerateMetadata = () => Promise<Metadata>;

/**
 * Compile in a server entry exporting this wording, and read every page's
 * metadata. One import at a time, after the reset: see `module.test.tsx`.
 */
async function metadataWith(metaMessages: unknown) {
  vi.resetModules();
  vi.doMock('@site-ui/server', () => ({ locale: 'fr', metaMessages }));
  const pages: Record<string, () => Promise<{ generateMetadata: GenerateMetadata }>> = {
    '/': () => import('@/app/page'),
    '/login': () => import('@/app/login/layout'),
    '/signup': () => import('@/app/signup/layout'),
    '/forgot-password': () => import('@/app/forgot-password/layout'),
    '/reset-password': () => import('@/app/reset-password/layout'),
    '/verify-email': () => import('@/app/verify-email/layout'),
    '/terms': () => import('@/app/terms/page'),
    '/team': () => import('@/app/team/page'),
  };
  const metadata: Record<string, Metadata> = {};
  for (const [path, load] of Object.entries(pages)) {
    metadata[path] = await (await load()).generateMetadata();
  }
  return metadata;
}

afterEach(() => {
  vi.doUnmock('@site-ui/server');
});

describe('public page titles', () => {
  it('use the module’s wording on the routes it may word', async () => {
    const metadata = await metadataWith({
      'meta.home.title': '{app_name}, en français',
      'meta.home.description': 'La page d’accueil de {app_name}.',
      'meta.login.title': 'Connexion',
      'meta.signup.description': 'Créer un compte sur {app_name}.',
      'meta.forgot.title': 'Mot de passe oublié',
      'meta.reset.title': 'Nouveau mot de passe',
      'meta.verify.title': 'Vérification',
      'meta.terms.title': 'Conditions',
      'meta.terms.description': 'Les conditions de {app_name}.',
    });

    expect(metadata['/']).toEqual({
      title: 'Runtime Console, en français',
      description: 'La page d’accueil de Runtime Console.',
    });
    expect(metadata['/login']).toEqual({ title: 'Connexion | Runtime Console' });
    expect(metadata['/signup']).toEqual({ description: 'Créer un compte sur Runtime Console.' });
    expect(metadata['/forgot-password']).toEqual({
      title: 'Mot de passe oublié | Runtime Console',
    });
    expect(metadata['/reset-password']).toEqual({
      title: 'Nouveau mot de passe | Runtime Console',
    });
    expect(metadata['/verify-email']).toEqual({ title: 'Vérification | Runtime Console' });
    expect(metadata['/terms']).toEqual({
      title: 'Conditions | Runtime Console',
      description: 'Les conditions de Runtime Console.',
    });
  });

  it('leave a console route in English, whatever the module’s dictionary holds', async () => {
    const metadata = await metadataWith({
      'meta.team.title': 'Équipe',
      'meta.team.description': 'Les personnes derrière {app_name}.',
      'team.eyebrow': 'Notre équipe',
      'chat.title': 'Discussion',
    });

    expect(metadata['/team']).toEqual({
      title: 'Team | Runtime Console',
      description: 'The people building Runtime Console.',
    });
  });

  it('drop undeclared keys and values that are not text', async () => {
    const metadata = await metadataWith({
      'meta.login.title': 42,
      'meta.signup.title': ['Inscription'],
      'meta.dashboard.title': 'Tableau de bord',
      toString: 'not a key',
    });

    expect(metadata['/login']).toEqual({});
    expect(metadata['/signup']).toEqual({});
  });

  it.each([
    ['declares no wording', undefined],
    ['words nothing', {}],
  ])('are the console’s when the module %s', async (_case, wording) => {
    const metadata = await metadataWith(wording);

    // Unchanged from a build with no module: the terms page's own title, and
    // the site's title and description — the root layout's — everywhere else.
    expect(metadata['/terms']).toEqual({
      title: 'Terms of Service | Runtime Console',
      description: 'Terms of Service for Runtime Console.',
    });
    for (const path of [
      '/',
      '/login',
      '/signup',
      '/forgot-password',
      '/reset-password',
      '/verify-email',
    ]) {
      expect(metadata[path], path).toEqual({});
    }
  });

  it('are declared for exactly the pages that read them', () => {
    // A key no page reads is wording a module can supply and no tab will show.
    const read = new Set<string>();
    const visit = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) visit(path);
        else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
          for (const match of readFileSync(path, 'utf8').matchAll(
            /publicPageMetadata\('(\w+)'\)/g,
          )) {
            read.add(match[1]);
          }
        }
      }
    };
    visit(join(__dirname, '..', 'app'));

    const declared = new Set(META_MESSAGE_KEYS.map((key) => key.split('.')[1]));
    expect([...read].sort()).toEqual([...declared].sort());
  });
});

/**
 * The generated server bridge, built for a stand-in module in a directory of
 * its own, so nothing the rest of the suite reads is rewritten.
 */
describe('the server bridge', () => {
  const frontend = join(__dirname, '..', '..');
  const require_ = createRequire(import.meta.url);
  const resolve = require_(join(frontend, 'src', 'site-ui', 'resolve.js')) as {
    generateSiteUi: (dir: string, env: Record<string, string>) => unknown;
  };

  function bridgeFor(serverSource: string) {
    const root = mkdtempSync(join(tmpdir(), 'site-ui-bridge-'));
    const app = join(root, 'frontend');
    const moduleDir = join(root, 'module');
    mkdirSync(join(app, 'src', 'site-ui'), { recursive: true });
    mkdirSync(moduleDir);
    copyFileSync(join(frontend, 'tsconfig.json'), join(app, 'tsconfig.json'));
    // The bridge checks the entry against the contract, next to it.
    copyFileSync(
      join(frontend, 'src', 'site-ui', 'contract.ts'),
      join(app, 'src', 'site-ui', 'contract.ts'),
    );
    writeFileSync(
      join(moduleDir, 'client.tsx'),
      "export const descriptor = { siteUiApi: 1, id: 'bridge', locale: '' } as const;\n",
    );
    writeFileSync(join(moduleDir, 'server.ts'), serverSource);
    writeFileSync(join(moduleDir, 'styles.css'), '');
    resolve.generateSiteUi(app, { SITE_UI_DIR: moduleDir, SITE_UI_API: '1' });
    return { root, bridge: join(app, 'src', 'site-ui', 'active', 'server.ts') };
  }

  function typeErrors(bridge: string): string[] {
    const config = ts.getParsedCommandLineOfConfigFile(
      join(frontend, 'tsconfig.json'),
      {},
      { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => undefined },
    );
    if (!config) throw new Error('tsconfig.json could not be read');
    const program = ts.createProgram([bridge], {
      ...config.options,
      noEmit: true,
      incremental: false,
    });
    return ts
      .getPreEmitDiagnostics(program)
      .map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'));
  }

  it('passes the module’s locale and wording through, and nothing else', () => {
    const { root, bridge } = bridgeFor('export {};\n');
    try {
      const { outputText } = ts.transpileModule(readFileSync(bridge, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS },
      });
      const exports = {};
      const wording = { 'meta.terms.title': 'Conditions' };
      runInNewContext(outputText, {
        exports,
        require: () => ({ locale: 'fr', metaMessages: wording, internal: true }),
      });

      expect(exports).toEqual({ locale: 'fr', metaMessages: wording });
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('fails the type check on wording that is not text', () => {
    const { root, bridge } = bridgeFor("export const metaMessages = { 'meta.terms.title': 42 };\n");
    try {
      const errors = typeErrors(bridge);

      expect(errors).toHaveLength(1);
      expect(errors[0]).toMatch(/is not assignable to type 'SiteUiServerModule/);
      expect(errors[0]).toMatch(/metaMessages\["meta\.terms\.title"\]/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }, 60_000);

  it.each([
    ['an empty entry', 'export {};\n'],
    [
      'wording beside other exports',
      "export const locale = 'fr';\n" +
        "export const metaMessages = { 'meta.terms.title': 'Conditions' };\n" +
        'export const internal = true;\n',
    ],
  ])(
    'type-checks %s',
    (_case, source) => {
      const { root, bridge } = bridgeFor(source);
      try {
        expect(typeErrors(bridge)).toEqual([]);
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    },
    60_000,
  );
});
