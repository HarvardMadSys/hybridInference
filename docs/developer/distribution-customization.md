# Distribution customization guide

A distribution can use the standard HybridInference website, apply its own
branding and operating policy, or build its own public pages while sharing the
console and authentication logic. Choose the smallest interface that covers
your requirements; each option has a different build and upgrade cost.

This guide describes the supported boundaries. The configuration references and
Site UI contract linked below define the individual fields and interfaces.

## Choose a customization level

| Approach | What you maintain | What upstream provides | When to choose it |
|---|---|---|---|
| Standard application | Deployment configuration, credentials and services | Default home, account and terms pages; dashboard, API-key management, Playground, request history and admin console | You want a working gateway and console with the default design |
| Configuration-only distribution | A distribution manifest, branding document, assets, models and deployment settings | The same page layouts and business logic, populated with your supported configuration | You need your name, links, models and operating policy |
| Public Site UI module | Configuration plus React components, scoped CSS, copy and assets for supported public pages | The console, account controllers, session and official frontend build recipe | You need a distinct website and account-page appearance |
| Source fork | Your changes to application source and their ongoing integration | The upstream source you choose to merge | Your requirements exceed the published configuration and module interfaces |

The standard application is a default implementation, not a hosted service.
Authentication, persistence, email, model providers and optional services still
need their documented deployment configuration. The
[Router Tutorial](router-tutorial.md) runs a local example from inference through
the Web/Admin Console.

## What a distribution can change

“Runtime configuration” below means configuration read by the running services;
it does not mean every file is watched or that an open browser updates live.
See [When changes take effect](#when-changes-take-effect).

| Area | Supported customization | Interface and limits | Compatibility owner |
|---|---|---|---|
| Site identity | Display name, public URL, support email, description, organization name/link/tagline and site host | Active distribution manifest plus its branding YAML; [Configuration](configuration.md#the-distribution-manifest) | Upstream owns the schema and consumers; the distribution owns values |
| Branding and content | Logo/favicon, docs/status/repository links, extra header links, quickstart API example, team/sponsors and data-policy notice | Versioned branding YAML; existing components decide where and how these appear | Upstream owns component behavior; the distribution owns content/assets |
| Home `/` | Replace the whole page, including navigation and footer; omit the replacement to keep the default home | Site UI `Landing`; [Public pages](site-ui.md) | Upstream owns API v1; the distribution owns its design |
| Account pages | Replace the outer layout, headings and supported wording; style shared fields and arrange the provided field nodes | Site UI `AuthFrame`, `authMessages` and form hooks; routes listed below | Upstream owns forms and authentication; the distribution owns presentation |
| Terms `/terms` | Supply a page frame and legal text, retaining the required terms/privacy anchors | Site UI `TermsFrame`; [Site UI contract](site-ui-api.md) | Upstream owns shared links/anchors; the distribution owns its published text |
| Shared console | Apply supported identity, links, assets and feature settings | Dashboard, settings, Playground, admin, `/chat`, `/team` and `/authorize` keep their shared layouts; Site UI cannot replace them | Upstream owns the shared UI; a distribution owns any source-fork changes |
| Additional pages | No module route registration in API v1 | A new path such as `/pricing` needs a shared application change or a separate site; adding a navigation link does not create a route | The implementer owns the additional integration |
| Models and routing | Models, endpoints, aliases, catalog metadata, credentials, weights and supported strategies | Registry/routing YAML, environment and supported admin APIs; [Adding models](adding-models.md), [Routing](routing.md) | Upstream owns configuration semantics; the distribution owns providers and policy |
| Account and feature policy | Registration availability, configured RAG access, and supported runtime settings | Manifest, environment and admin settings, according to each feature's backend rules | Upstream enforces policy; the distribution selects supported values |
| Backend extensions | Trusted deployment-local adapter factories and documented registry extensions | `BACKEND_EXTENSIONS`; [Backend extensions](configuration.md#backend-extensions) | Upstream owns documented hooks; the distribution maintains its extension code |

The five account routes are `/login`, `/signup`, `/forgot-password`,
`/reset-password` and `/verify-email`. Their required fields, validation,
submission, verification, session handling, redirects and permission checks
remain in the shared application. Changing their appearance does not add a new
login method or change account approval policy.

There is no configuration field for an arbitrary home-page hero and no public
interface for replacing the console's overall theme, sidebar or layout. A
module's CSS must be scoped to its own pages; global selectors that restyle the
console are outside the supported customization boundary.

A module can provide its own copy and override the message keys exposed by
`authMessages`. Unspecified keys use the shared defaults. The server `locale`
sets the document language; it does not translate the console. A localized home
page therefore does not imply a fully localized product.

## Configuration is the first option

Start with the annotated `config/examples/distribution.example.yaml` and
`config/examples/branding.example.yaml`. Keep deployment-owned files in your
distribution repository or overlay, with credentials in the environment.

- Set the site's name in `distribution.display_name`, and its URL and support
  address under `site`. Copy the complete branding template and point
  `site.branding` at it; a display name alone, without a branding document, does
  not replace the frontend's default name.
- Set `organization`, `links`, `example`, `assets`, `team`, `sponsors` and
  `data_policy_notice` in the branding document as required. These are content
  inputs to the default components, not arbitrary layout instructions. Extra
  `links.nav` entries add HTTPS header links; they do not replace navigation.
  Team entries appear on `/team`, and sponsor entries on the default home page.
- Serve local branding images through the mounted site-assets directory and
  `/site-assets/*`, or use permitted public asset URLs. Check the URLs over HTTP.
- Select model/routing files through the documented resolver, and keep provider
  credentials in environment variables referenced by the registry.
- Set `DISTRIBUTION_CONFIG_PATH` and explicitly select
  `DISTRIBUTION_CONFIG_MODE=active` when ready to apply the manifest. The default
  `dark` mode validates and compares it without activating its settings.

The active backend exposes the permitted public subset through `/site-config`;
the frontend uses it for identity and presentation. A failed or invalid
response produces a configuration error instead of silently rendering a
working site with different defaults. See
[Deployment](deployment.md#what-a-change-actually-requires).

Some similarly named settings have different jobs:

| Setting | Actual effect |
|---|---|
| `features.public_signup: false` in an active manifest | Closes both public registration UI and API; runtime `signup_enabled=true` cannot override it |
| `features.public_signup: true` or unset/null | Defers to the effective runtime/environment signup policy; it does not bypass verification or approval |
| `features.rag: false` in an active manifest | Disables the configured RAG UI and backend access; enabling it still requires the RAG service's configuration |
| `features.routers` | Declared/public metadata; it does not select or restrict the model router. Use the model's `router` and supported admin overrides |
| `AGENT_PUBLIC_URL`, or the internal agent service destinations | Configure the existing agent link/proxy integration; they do not install an agent service or make `/agents` a Site UI slot |
| `site.terms_document` / `site.privacy_document` | Accepted manifest fields, but not a loader for the current terms page. Use the Site UI legal-page interface to publish custom text |

For exact signup precedence, see
[The distribution manifest](configuration.md#the-distribution-manifest); for RAG,
see [Docs RAG Assistant](rag-chat.md); for service paths, see
[The public path table](public-path-table.md). A public branding hint or hidden
navigation item is not a substitute for backend authorization.

Model/provider changes made through supported admin APIs take effect on the
running gateway and are persisted in its database. They do not rewrite the YAML;
stored overrides are applied on top of it again at startup. Account for both
layers when investigating a configuration difference. See
[Runtime configuration](configuration.md#runtime-configuration-from-the-admin-console).

Backend extensions are trusted Python code loaded at startup, not a general UI
or authentication plugin system. Use the documented adapter, setting, provider
metadata, agent-access and quota registration hooks; a new authentication flow or arbitrary
business endpoint needs its own implementation and review.

## When changes take effect

| Change | Required action | Frontend image rebuild? |
|---|---|---|
| Mounted distribution manifest, branding YAML, model/routing files | Restart the backend to load the changed files; reload the page to check presentation | No |
| A mounted site-assets file | Replace it in the overlay; verify the served URL (the response allows five minutes of browser caching) | No |
| Runtime environment, including server-only frontend destinations | Recreate the affected container with the new environment; a plain restart retains the old container environment | No, for runtime values |
| Supported admin model/provider/routing/settings edit | Save through the admin console/API; check the effective value and any feature-specific caching | No |
| Site UI components, compiled copy, CSS or packaged assets | Build and deploy a new frontend image containing that module | Yes |
| A true build-only `NEXT_PUBLIC_*` value or the backend network target compiled from `BACKEND_INTERNAL_URL` | Rebuild and deploy the frontend | Yes |
| Source or extension code | Rebuild the affected image, or restart the backend for an already-mounted importable extension | Depends on where the code is packaged |

Changing `SITE_ASSETS_DIR` itself requires recreating the frontend container.
Reusing a standard frontend image also assumes its compiled backend network
target matches your deployment; that target is separate from site branding.
Use runtime branding for identity rather than introducing new build-time
branding variables. The precise Compose commands are in
[Deployment](deployment.md#what-a-change-actually-requires).

## Three implementation paths

### 1. Keep the standard UI and configure a distribution

1. Start from the [example distribution](configuration.md#the-example-distribution)
   and its tutorial. Replace the teaching provider, local-only credentials and
   service settings for your environment.
2. Create your manifest, branding YAML and model/routing configuration; mount
   the files and branding assets in the appropriate services.
3. Use the standard frontend image with no Site UI selection. Validate the
   manifest in dark mode, activate it, then recreate/restart services as above.
4. Check `/site-config`, the home and account pages, branding URLs and the
   authenticated console. Exercise registration policy and a model request.

This path needs no React implementation and keeps the upstream page layouts.

### 2. Build distinct public pages with Site UI

1. Complete the configuration path first; a custom module still needs the
   backend's runtime configuration and shared services.
2. Follow [Distribution-owned public pages](site-ui.md) to create an API v1
   module with its manifest, required client entry and stylesheet. Export only
   the page components you want to replace; omitted components keep defaults.
3. Import shared capabilities only from `@site-ui/host`, alongside permitted
   framework and module-local imports. Preserve shared form nodes, accessibility
   hooks, terms anchors and account navigation.
4. Build with the official frontend Dockerfile, the `site-ui` named context and
   `SITE_UI_API=1`. Pin the upstream source and module source used for release.
5. Deploy the resulting frontend with the configured backend. Test every owned
   route, asset and form state, and check the shared console for style leakage.

The module is compiled into the same Next.js application; it is not fetched or
swapped at runtime. An absent module selects the standard UI. A selected module
with missing required files, a mismatched API version or unsupported imports
fails the build. Module content changes require a new image; runtime branding
values read by the module retain their runtime behavior.

### 3. Upgrade an existing source fork

1. Inventory local changes by route and behavior. Separate branding values,
   public-page presentation, shared-console changes and backend logic.
2. Where they fit, move values into runtime configuration and public-page
   presentation into a Site UI module. Migration is optional; you can keep the
   fork, with its ongoing merge and verification work. A UI module keeps
   authentication controllers shared.
3. Identify remaining source changes explicitly. Console redesigns, extra
   routes and new authentication behavior do not become supported module
   features merely by moving files into the module directory.
4. Merge the intended upstream version in an isolated branch, resolve conflicts
   against your required behavior, and build the exact frontend/backend pair.
5. Verify the distribution in staging before promoting it. A successful merge
   or upstream CI result alone does not verify a fork's presentation or flows.

Adopting Site UI can reduce future conflicts; it does not automatically migrate
an existing fork or make arbitrary upstream source paths stable interfaces.

## Upgrade and compatibility checklist

Upstream maintains the standard implementation, configuration consumers and
published Site UI contract. Within API v1, optional additions are compatible;
removing exports, changing prop meanings or repurposing semantic hooks requires
an API revision and migration. The deprecated `authAppearance` class map remains
supported throughout v1. See [Compatibility](site-ui.md#6-compatibility).

Each distribution maintains its configuration, module, deployment and any fork
changes. Before promotion:

- Pin the upstream version and, for custom UI, the module version and produced
  image digest. Keep the matching configuration and asset versions recoverable.
- Read configuration/API migration notes and validate against the selected
  version. Unsupported module API revisions must stop the build.
- Inspect the actual image's `/app/site-ui-manifest.json` to confirm the intended
  standard or custom build; test packaged and mounted assets over HTTP.
- Check public routes, configured links, narrow/wide layouts, keyboard access,
  form validation/loading/errors, login/logout and the registration policy.
- Check the authenticated console, user/admin permissions and an inference
  request against the deployed candidate, including any local fork behavior.
- Promote only the tested image/configuration combination and retain the prior
  combination for rollback. Upstream merging a change does not itself define
  when a distribution upgrades; that is its release pipeline's responsibility.
