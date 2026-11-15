# Enterprise single sign-on (OIDC)

- Date: 2026-11-13
- Status: proposed, awaiting review. Implemented by the two pull requests in §15
- Issue: #1719 (OIDC first, SAML later)
- Scope: sign-in to the browser console, automatic account creation through SSO, account linking, and
  confirmation of sensitive actions. Unchanged: API key authentication, the `/v1` inference path, and the cloud
  agent's identity contract (`/v1/identity/*`, where this gateway is the identity provider, the opposite
  direction from this record)
- Based on: `dev` @ `a159ec82`
- Related records: [database-only configuration](2026-11-08-database-only-configuration.md) (configuration
  documents and `${NAME}` secret references),
  [versioned schema migrations](2026-11-02-versioned-schema-migrations-design.zh.md) (new tables are expand-only),
  [configuration hot reload](2026-10-27-unified-config-hot-reload.zh-CN.md),
  [public path table](../../developer/public-path-table.md),
  [console language switching](2026-10-17-console-language-switching-design.zh.md)

## 1. Background and goals

The console has two ways to sign in today: an email or login name with a password, and a phone number with an
SMS code. A member of a university or a company cannot sign in with the organization's account. They register
a separate account, and the operator approves each one by hand.

An organization's deployment needs two things: members sign in with the organization's account, and the
organization's identity system (the IdP below) is what confirms "this person is one of ours", so the operator
no longer approves people one by one.

HybridInference is an open-source gateway that each operator deploys, and every deployment connects a different
IdP. So this record describes a **standard OIDC client**: the operator enters the IdP's details in Admin and it
works. No specific IdP appears in the code.

Goals:

1. A user can sign in to the console with the organization's account.
2. For each IdP the operator decides three things: whether accounts are created automatically, whether a new
   account needs approval, and whether an existing account is linked by email without asking.
3. A user who already has an account can link the organization's account to it, and nobody else can take that
   account over by using the same email.
4. An account that signs in only through SSO can still delete the account, set a password and bind an email,
   the three actions that need confirmation.
5. A deployment with no SSO configured behaves exactly as before.

The three switches and guided linking follow GitLab, which is also self-hosted and connected to an
organization's IdP, and leaves the choice to the operator through `allow_single_sign_on`,
`block_auto_created_users` and `auto_link_user`. §6.2 and §7 say where this design differs.

## 2. Not in this work

| Left out | Note |
|---|---|
| SAML 2.0 and the CAS protocol | Follow-up pull requests, with native support. The code is shaped for them (§12) |
| Forcing a domain to use SSO only (no password sign-in) | Later. On its own it does not stop an account the IdP has disabled, see §10 |
| Entering this site from the organization's portal (sign-in started by the IdP) | No protocol support for it. Putting the link of §5.3 on the portal has the same effect |
| Single logout (signing out here also ends the user's session at the IdP) | Later. Like most sites, v1 signs out of this site only |
| Roles assigned from the IdP's groups | Later. In v1 each IdP has one default role |
| Revoking access automatically when the IdP disables an account (SCIM push, or periodic re-verification with the IdP) | Follow-up pull request. §10 has v1's behavior and what to do |
| Creating the account only after the email is verified at signup | Suggested as a follow-up. An unverified registration holds its email today, see §10 |
| A dedicated SSO form in Admin | v1 edits the JSON configuration document directly, like every other document |
| Multi-tenant issuers (Entra's `common`, for example), `private_key_jwt`, encrypted ID tokens | When there is a need |

## 3. The flow

```text
Browser                Console (Next.js)            Gateway /auth/sso/*               IdP
  │ click "Log in with SSO", type an email │              │                               │
  │ POST /auth/sso/discover ────(relayed as is)────────▶ │ pick the IdP by email domain,  │
  │                                                     │ insert an sso_flows row        │
  │ ◀──────── the IdP's authorization URL, flow cookie ─ │                               │
  │ go to the authorization URL ────────────────────────────────────────────────────▶ │ user signs in at the IdP
  │ ◀──────────────────────────── 302 /auth/sso/xx/callback?code=…&state=… ────────── │
  │ GET callback ───────────────(relayed as is)────────▶ │ check state, exchange the code │
  │                                                     │ validate the ID token,         │
  │                                                     │ resolve the account (§6)       │
  │                                                     │ start the session              │
  │                                                     │ (start_user_session)           │
  │ ◀──── 302 /login?sso=complete&next=… (session cookie) │                              │
  │ the sign-in page takes up the session, then goes to next │                           │
```

Neither the proxy nor the public path table changes:

- The console already relays `/auth/*` to the gateway as is. The relay does not follow a 302, and it passes each
  `Set-Cookie` header through (`apps/frontend/src/lib/server/gatewayProxy.ts`).
- In `deploy/public-paths.json`, `/auth` is a prefix owned by the gateway, so the new `/auth/sso/*` is already
  covered.
- The console adds no public route. Completing a sign-in and confirming a link are states of the `/login` page;
  §5.1 says why.

## 4. Configuration

### 4.1 A new configuration document, `SSO_PROVIDERS`

It is added the way the other documents are: `"sso": "SSO_PROVIDERS"` in `DOCUMENT_KEYS` in
`serving/config/documents.py`, registered in `app_config_registry.py` under the `application` category with
`restart_required=False`. The model and its validation are in a new module, `serving/config/sso.py`. The document
therefore gets validation before saving, conditional writes, audit, backup and restore. Every setting in the
`application` category is security-related, so changing it asks the administrator for their password.

```json
{
  "providers": [{
    "id": "example-univ", "type": "oidc", "display_name": "Example University", "enabled": true,
    "issuer": "https://idp.example.edu/realms/main",
    "client_id": "hybridinference", "client_secret": "${SSO_EXAMPLE_UNIV_CLIENT_SECRET}",
    "client_auth_method": "client_secret_basic",
    "scopes": ["openid", "email", "profile"],
    "domains": ["example.edu", "*.example.edu"],
    "assume_email_verified": false,
    "auto_create": false, "require_approval": true, "auto_link": false,
    "default_role": "free"
  }]
}
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Short name, `[a-z0-9-]`, at most 32 characters. It appears in the callback address, so renaming it means changing the callback address registered at the IdP too |
| `type` | required | The protocol. v1 accepts only `oidc`. SAML and CAS add values later, and existing configuration does not change |
| `display_name` | required | The name shown in prompts, such as the link confirmation and the account switcher. One value; it does not follow the console's language |
| `enabled` | `true` | When off, every entry to this IdP is unavailable and its linked identities are kept |
| `issuer` | required | Must equal the `issuer` in the discovery document exactly. https is required; only `localhost` may use http |
| `client_id` / `client_secret` | required | `client_secret` can only be written as `${NAME}`, naming a separate secret setting (§4.2) |
| `client_auth_method` | `client_secret_basic` | How the client proves itself to the token endpoint; `client_secret_post` is the other choice |
| `scopes` | `["openid","email","profile"]` | Must include `openid` |
| `domains` | required, not empty | The email domains this IdP answers for; a sign-in finds its IdP by them (§5.2). Same rule as `signup_allowed_domains`: an exact match, and `*.x` matches only subdomains of x |
| `assume_email_verified` | `false` | Some IdPs do not return `email_verified` (Entra ID does not by default). An operator who knows the organization manages these addresses can turn it on |
| `auto_create` | `false` | Whether an account is created on a first SSO sign-in when none exists here |
| `require_approval` | `true` | Whether an automatically created account waits for approval first |
| `auto_link` | `false` | Whether an existing account with the same email is linked without confirmation (conditions in §6.2) |
| `default_role` | `free` | The role of a new account: `free`, `pro` or `internal`, never `admin` |

- **Only five fields are specific to OIDC**: `issuer`, `client_id`, `client_secret`, `client_auth_method` and
  `scopes`. The rest do not depend on the protocol and apply to another protocol as they are.
- **A field name cannot contain `token`, `secret` or `password`** unless its value really is a secret reference.
  `documents.py` treats every field with such a name as a secret and requires `${NAME}`. That is why the client
  authentication method is `client_auth_method`, not the specification's `token_endpoint_auth_method`.
- **`domains` is required**: an email is trusted only when its domain belongs to this IdP (§6.1), so with no
  domain nobody could complete a first sign-in in v1.
- **Validation at save time looks at this document only.** It sends no request and reads no other setting: an
  `id` cannot repeat, and the `domains` of different IdPs cannot overlap. Whether the IdP can be reached is known
  only on first use; if not, the sign-in reports `unavailable` and the reason is logged.

### 4.2 Secrets

`documents.py` already requires that a field whose name contains `secret` be a `${NAME}` reference and refuses
a literal value. `discover_documents` is extended so that the references in `SSO_PROVIDERS` also appear among
the secrets Admin asks for (Settings → Access, the Security section).

### 4.3 When a change applies

- `SSO_PROVIDERS` itself applies as soon as it is saved, through the existing hot reload (it is edited under
  Settings → Models, the Application section).
- The client secret it names is read only when the gateway starts, like every secret a configuration document
  references: after it is entered or changed, the gateway must restart, and Admin marks it Restart required. So
  the order for a first setup is: save the IdP configuration, enter the secret, restart. Until the restart, the
  sign-in page already shows the SSO entry, and using it reports that SSO is temporarily unavailable.
- Each issuer's discovery document and JWKS are cached in each process's memory for one hour; any configuration
  change clears them all. Keys are fetched one address at a time, so an IdP whose key address answers slowly
  holds up only its own sign-ins.

### 4.4 What the console is told

`/site-config` is a public endpoint that needs no sign-in. One `sso` block is added at the **top level** of its
response:

```json
{"sso": {"enabled": true}}
```

- The block is absent when no IdP is enabled. It says only "SSO is on" and reveals nothing about any IdP,
  including which organizations there are.
- No key is added to `features`. The console parses `features` strictly, so one extra key would make an older
  console reject the whole document; it parses top-level fields loosely, so an older console ignores the `sso`
  block. `setup`, `configuration` and `phone_auth_policy` are at the top level for the same reason.

### 4.5 The callback address

`redirect_uri = {FRONTEND_URL}/auth/sso/{id}/callback`. This address is registered at the IdP.

- `FRONTEND_URL` (the console's public address) is used because the session cookie set on the callback has to
  land on the console's domain. The links in verification and password-reset emails already use this setting, so
  a deployment whose emails work needs no new configuration.
- The address cannot be derived from the request's Host header, which can be forged.
- When `FRONTEND_URL` is not set or is not an absolute address, the SSO entry returns `unavailable` at run time
  and logs the reason.
- Users must reach the console through `FRONTEND_URL`. From another domain, the flow cookie does not come back
  with the callback and the sign-in reports `expired`.

## 5. Where a sign-in starts

### 5.1 The sign-in page

```text
┌──────────────────────────────────┐
│ Email or login name [          ]  │
│ Password            [          ]  │
│ [            Log in            ]  │
│               or                 │
│         Log in with SSO          │  ← the only entry, shown when SSO is on
└──────────────────────────────────┘
After "Log in with SSO":
│ Work or school email [ alice@example.edu ]  [Continue] │  → IdP found by email domain → redirect
```

- The sign-in page has one "Log in with SSO" entry. The user opens it and types an email; the gateway finds the
  IdP by the email's domain and sends the browser there.
- There is no button per IdP. The page then never lists which organizations are connected, and the flow is the
  same however many IdPs there are.
- v1 changes the sign-in page only. With automatic account creation on, a member's first sign-in is their
  registration, so the registration page needs no entry of its own.

**Site UI contract.** The sign-in page is a public page whose appearance a distribution can replace with a Site
UI module (`apps/frontend/src/site-ui/contract.ts`). The shared controller renders the page's logic; the module
supplies the frame and the styles. v1 stays inside the current contract version:

- New elements use only classes `AuthAppearance` already has (`linkButton`, `hint`, `input`, `submit`,
  `notice*`). The contract marks this class map as deprecated, so no member is added to it.
- The entry carries `data-auth="sso"`, which a module can select to style it. The contract asks new styling to go
  through `data-auth`.
- The secondary links under the form ("Back to log in", "Not now") sit in a row of their own outside the form
  (`data-auth="secondary-actions"`). The page gives that row its spacing, because a distribution's form class
  does not necessarily space its children.
- A distribution's skin lives in its own private repository and cannot be built here. It was checked this way:
  unit tests pin the markup the page produces under a custom class map, and the same markup was placed in the
  real styles of a live distribution's sign-in page, where the entry, the email step and the link confirmation
  all displayed correctly.
- The keys of the new strings are added to `AUTH_MESSAGE_KEYS`. A key the module does not translate falls back
  to the shared string.
- No public route is added. `PUBLIC_ROUTES` is a closed table, and a new page outside it can only have the shared
  console's appearance, which would not match the module's sign-in page. So completing a sign-in and confirming
  a link are states of the `/login` page (`?sso=complete`, `?sso=link`).

**Strings.** They follow the console's rule, `t(slot, English)`: English is the source text, Chinese goes in
`apps/frontend/src/lib/i18n/messages/zh-CN.json`, and variables are filled in with `fill()`. The slots are
`auth.sso.*`, for example `auth.sso.button` (Log in with SSO) and `auth.sso.error.<code>`. The full set of
strings is in the pull request that implements them.

### 5.2 `POST /auth/sso/discover`

The endpoint the "Log in with SSO" entry calls. The body is `{"email": "...", "return_to": "..."}`; the email is
in the body, so it does not reach the access log.

1. Find the IdP by the email's domain. If there is none, or it is not enabled, return `{"available": false}` and
   the page says SSO is not set up for this email.
2. Insert an `sso_flows` row holding the state, nonce, PKCE verifier and `return_to`.
3. Set the flow cookie (the session cookie's name with `_sso_flow_` and the flow's purpose appended, here
   `login`): a random value, HttpOnly, path `/auth/sso`, SameSite=Lax, valid for 10 minutes.
4. Return `{"authorization_url": "..."}`, and the page goes there. The address carries `state`, `nonce` and
   `code_challenge` (S256), and passes the email to the IdP as `login_hint` so the user does not type it again.

Constraints:

- This endpoint never reads the user table, so it cannot reveal which emails are registered.
- Every call writes a database row, so it is rate limited by client address: 120 calls per address per hour
  (`serving/utils/sso_rate_limit.py`). No credential is checked here, so the limit is looser than the sign-in
  limit and enough for many people behind one campus address. Over the limit it returns 429.
- Domain matching follows the signup allow-list's rule and is a pure function in `serving/config/sso.py`.
- An IdP that is found but cannot be reached, or whose secret is not entered yet, returns 503.
- Before first-run setup is complete it returns 503, as registration does. Every other entry in this record is
  unavailable then too.
- Each purpose (`login`, `reauth`, `link`) has a flow cookie of its own, and each holds one value. When a
  sign-in is started in two tabs one after the other, the earlier one reports `expired`; starting it again
  works. A sign-in can be started by a plain link from any site (§5.3). With its own cookie it cannot end a
  link (§7.2) or a confirmation (§9) that a signed-in user is in the middle of.

### 5.3 `GET /auth/sso/{id}/start?return_to=…`

A link that goes straight to one IdP without the email step. The sign-in page does not show it. It has two uses:

- An organization puts it on its own portal as the way into the platform.
- The account switcher signs an SSO account in again (§10). The browser already knows which IdP the account
  belongs to, so it does not ask for the email.

It does what steps 2 and 3 of §5.2 do, then answers with a 302 to the IdP's authorization address. When the IdP
is not enabled, it answers with a 302 to `/login?sso_error=unavailable`. It is an ordinary link and does not
depend on JavaScript. It is rate limited by client address as well; over the limit it returns to the sign-in
page with `sso_error=rate_limited`.

## 6. The callback and account resolution

### 6.1 Checks on the callback

They run in order. A failure at any step answers with a 302 to `/login?sso_error=<code>`.

1. State: claim the `sso_flows` row. A request that cannot claim it gets `expired`. The claim is one atomic
   `UPDATE … WHERE … RETURNING` whose conditions are all of:
   - The hash of the state is the row's, and the row is unused and not expired. So a row is used once, and an
     expired row cannot be claimed.
   - The request carries the row's flow cookie.
   - The `{id}` in the callback path equals the row's IdP. With several IdPs configured, this stops one IdP's
     callback from being taken for another's.

   A request that fails any of them changes nothing: the row stays good for the browser that started the
   flow, and no cookie is cleared. So a `state` value that leaks cannot be used from another browser to use
   the flow up.
2. The IdP sent back an `error` (the user cancelled, for example): `cancelled`.
3. Exchange the code: a POST to the token endpoint with `code`, `redirect_uri` and `code_verifier`, following
   no redirect. Every endpoint address the discovery document gives must be https (`localhost` excepted).
4. Validate the ID token as OIDC Core 1.0 §3.1.3.7 says: signature, `iss`, `aud`, `azp`, `exp` and `nonce`.
   Only the RS, PS and ES signature algorithms are accepted; `none` and HS* are refused.
5. Read the claims: `sub` is required. `email`, `email_verified` and `name` come from the ID token first; with
   no `email` there, the userinfo endpoint is asked, and its `sub` must equal the ID token's.
6. Decide whether the email is trusted. Both must hold. Emails are lowercased before any comparison.
   - There is an email and its domain is one of this IdP's `domains`.
   - `email_verified` is true, or this IdP has `assume_email_verified` on.

The domain has to be checked as well because an IdP can vouch only for the domains it answers for. "Trust the
email whenever the IdP says it is verified" is where vulnerabilities of the nOAuth kind (2023) come from.

The access token and refresh token the IdP returns are discarded after use and never stored.

### 6.2 Account resolution

| # | Case | Result |
|---|---|---|
| 1 | (issuer, sub) is already linked to an account | Sign in after the status check of §6.3 |
| 2 | Not linked; the email is not trusted | Refuse, `email_untrusted` |
| 3 | Not linked; trusted email; an account here has this email but its status is not `active` | Refuse by the status of §6.3; no link, no new account |
| 4 | Not linked; trusted email; the `active` account with this email is already linked to another identity of the same issuer | Refuse, `identity_conflict` |
| 5 | Not linked; trusted email; the `active` account with this email meets every condition for automatic linking below | Link and sign in |
| 6 | Not linked; trusted email; there is an `active` account with this email, any other case | Guided linking (§7.1) |
| 7 | Not linked; trusted email; no account here has this email; `auto_create` is on | Create the account (§8) |
| 8 | As row 7, with `auto_create` off | Refuse, `no_account` |

"An account here has this email" means `get_user_by_email` finds one, whatever its status.

**Every condition for automatic linking (row 5):**

- this IdP has `auto_link` on;
- the deployment currently requires email verification at signup (`email_verification_required()` is true);
- the account here has `email_verified` true;
- the account here is not an administrator: its role is not `admin` and its email is not in `ADMIN_EMAILS`.
  Such accounts always go through guided linking (§8 says why).

**Why "the email is verified here" is not enough.** When a deployment turns email verification off, signup marks
a new account verified at once, and the mark does not mean the address was ever verified. On such a deployment
someone can register with a victim's email and set a password first; with automatic linking, the victim's
first SSO sign-in would merge them into that account. Two kinds of deployment should not turn `auto_link` on
even when the conditions above hold, and the operator guide says so:

- A deployment that has ever had email verification off. Accounts registered then carry the same "verified"
  mark, and they cannot be told apart afterwards.
- An organization that reassigns email addresses. A newcomer who receives an old address would be merged into
  the previous owner's account.

Other notes:

- The email is used only while the identity is not linked yet. Once linked, only (issuer, sub) counts, and a
  user changing their email at the IdP has no effect.
- The difference from GitLab: with `auto_link_user` on, GitLab merges whenever the emails match. This design
  adds the four conditions above.
- Row 3: deleting an account is a soft delete and the email stays on the row; the existing registration endpoint
  refuses such an email too. So a deleted account's email cannot be revived through SSO, which matches how
  registration behaves today.
- Row 4: this happens when the IdP recreated the user and the `sub` changed, and it can also mean the address
  was given to someone else. The gateway cannot tell the two apart, so it replaces nothing; an administrator
  confirms and unlinks the old identity in Admin (§7.3).
- Concurrency: when two callbacks for the same new identity arrive together, `UNIQUE (issuer, subject)` lets one
  write succeed, and the other reads again and signs in to the same account. The write that links checks in the
  same transaction that the account is still `active`. How this excludes the hard-delete work in progress
  (#1733, #1782) is aligned with that work when it lands.

### 6.3 Account status

The checks match password sign-in and reuse its code: `pending_approval` gives `pending`, `suspended` gives
`suspended`, and `rejected` and `deleted` give `inactive`.

### 6.4 Starting the session

The existing `start_user_session` (`serving/servers/routers/auth_routes.py`) is called directly; password
sign-in and first-run setup already share it. The callback has to answer with a redirect, so the redirect
response is passed in and the cookies are set on it. Successes and failures are written to `login_events` as
usual, and a failure's reason starts with `sso_`. When the browser already holds the maximum number of
signed-in accounts, the callback returns to the sign-in page with `sso_error=browser_full`.

Then it answers with a 302 to `/login?sso=complete&next=<return_to>`. In this state the sign-in page does what
`startSession` does today (clear the query cache, refresh the user and the account list, tell other tabs) and
then goes to `next`. The access token lives in the page's memory, so this step fetches it with the session
cookie from the existing `/auth/refresh`.

`return_to` is checked again on the server and accepts only a path inside the site, by the same rule as the
console's `internalPathOr`. The default is `/dashboard`.

A user who signed in through SSO can, like any signed-in user, obtain identity credentials for a separate
service such as the cloud agent through `/v1/identity/*`. Nothing changes there.

### 6.5 How errors come back

A failed sign-in returns to the sign-in page as `/login?sso_error=<code>`. Confirmation (§9) and linking from
Settings (§7.2) are started by a signed-in user, and a failure returns to the page that started it, with
`sso_error` as well. The codes are the ones each section gives; any failure not listed there is `failed`.

The URL carries the code only, and none of the details the IdP returned. Details go to the server log, which
must never contain a code, token, state, nonce or verifier. The callback's query holds the code and the state,
and uvicorn's access log records the whole address, so a filter is added to the access log
(`serving/utils/logging.py`) that records only the path of a callback request.

## 7. Account linking

There are three ways to link: guided linking at sign-in, linking and unlinking by the user in Settings, and
viewing and unlinking by an administrator.

### 7.1 Guided linking at sign-in (an account here has the same email)

1. When the callback reaches row 6 of §6.2, it inserts an `sso_pending_links` row holding the IdP id, issuer,
   sub and email, valid for 10 minutes, and sets the HttpOnly link cookie (the session cookie's name with
   `_sso_link` appended, SameSite=Lax). Then it answers with a 302 to `/login?sso=link`.
2. In this state the sign-in page does **not** take its default step of leaving when already signed in. It first
   reads the pending identity with `GET /auth/sso/link` (read by the cookie; the email is not in the URL). If
   nothing can be read it shows `expired`.
3. Then one of two cases:
   - The account this browser is signed in to has the pending email: go to step 4.
   - Otherwise (not signed in, or signed in to another account): show the sign-in form with the email filled in
     and ask the user to sign in once the way they used to. A password or a phone code both work, and the
     existing sign-in endpoints do not change at all. After a successful sign-in, go to step 4.
4. A confirmation panel shows the IdP account to be linked and the current account, with "Connect" and
   "Not now".
5. "Connect" calls `POST /auth/sso/link/confirm`. The endpoint authenticates with the Bearer header and also
   requires the link cookie. The current account's email must equal the pending email (case-insensitive), and
   the account must not be linked to another identity of the same issuer. Then the link is written and the
   pending row and the cookie are removed. "Not now" calls `POST /auth/sso/link/cancel`, which discards the
   pending data.

Reasons:

- **Why this stops a squatted registration.** Someone registered with the victim's email and set a password
  first. When the victim signs in through SSO they do not enter that account; they are asked to prove they own
  it. The victim does not know the password, but the mailbox is theirs, so they can reset it with "forgot
  password", and a reset ends every session of the account. A reset does not revoke existing API keys, so the
  confirmation panel has a line reminding the user to review the account's API keys.
- **Why the emails must match.** Consider a shared computer: A starts an SSO sign-in and does not finish; within
  10 minutes B signs in to their own account in the same browser and clicks "Connect" without reading. If the
  emails did not have to match, A's organization account would be linked to B's account, and A could sign in to
  B's account through SSO from then on.
- **Why linking is not folded into the sign-in endpoints.** The existing sign-in endpoints stay untouched, and
  the link gets an explicit confirmation of its own. GitLab also shows a separate "authorize linking" page
  after sign-in.

### 7.2 Linking and unlinking in Settings

Account settings gain an SSO section that lists the linked identities (IdP name, the email at the time of
linking, when it was linked) and offers "Connect an SSO account" and "Disconnect". It covers what guided
linking cannot: the platform account's email differs from the organization's email.

**Linking:**

1. The user types a work or school email and confirms who they are: an account with a password types it, and an
   account that uses SSO only confirms at the IdP first (§9). Linking gives the account another way to sign in,
   so it is confirmed first, as binding an email is.
2. `POST /auth/sso/identities/link` (Bearer header) finds the IdP by the email's domain, inserts an `sso_flows`
   row with the purpose `link` that records the user id and the `sid`, and returns the IdP's authorization
   address. The IdP is found before the confirmation is checked: an email no IdP answers for returns
   `{"available": false}` and does not spend a confirmation made at the IdP, which is good only once.
3. Besides the checks of §6.1, the callback checks three things. When they pass it writes the link and returns
   to Settings.
   - This (issuer, sub) is not linked to another account; otherwise `identity_in_use`.
   - The organization's email is not another account's email; otherwise `email_in_use`.
   - The current account is not linked to another identity of the same issuer; otherwise `identity_conflict`.

The organization's email does not have to equal the platform account's email here: the person who started it is
signed in and passed the confirmation, and then signed in at the IdP, so both sides have shown it is the same
person. But linking is refused when the organization's email is already another account's email: one email
belongs to one account, and the organization account is linked inside that account (guided linking at sign-in,
or Settings after signing in to that account).

**Unlinking:** `POST /auth/sso/identities/{id}/unlink`, also after confirming who the user is. When this link is
the account's only way to sign in (no password, no verified phone, no other link) it is refused, and the user
is told to set a password first. Settings shows that message in this case instead of the Disconnect button, so
the user does not make a pointless trip to the IdP.

**Neither action accepts a phone code.** The purposes of a code are a fixed set of values in the database;
widening it changes a constraint, and the migration rules do not allow a constraint change in an expand-only
revision. So an account with a phone and no password sets a password first (which can itself be confirmed with
a phone code), then links or unlinks. Administrator confirmation treats phone accounts the same way.

Confirmation at the IdP uses the purposes `link_sso` and `unlink_sso`. Linked identities are read with
`GET /auth/sso/identities`.

### 7.3 Viewing and unlinking by an administrator

The user detail in Admin lists the identities linked to the account (IdP name, the email at the time of
linking, when it was linked, the last SSO sign-in), each with "Disconnect". It is for two cases: a wrong link,
and `identity_conflict`.

- Unlinking asks the administrator for their password (`confirm_administrator`), like promoting a role. Each
  one is audited. When the link was removed by another request meanwhile, the answer is 404 and nothing is
  audited.
- Unlinking removes the link only and leaves the account's sessions and API keys alone. To cut off access,
  suspend the account.
- An account that uses SSO only has no way to sign in after unlinking; the administrator is told before acting.
  The user can set a password afterwards with "forgot password".
- An administrator cannot create a link. A link is made only by the account's owner signing in at the IdP.

## 8. Automatic account creation and approval

- The account and its link are written in one transaction (`create_sso_user`): an account without the link is
  one its owner could never sign in to again. It works like creating a phone account (`create_phone_user`) and
  creates the account's Default project too.
- The new account's fields: `email` is the trusted email the IdP returned, `email_verified = true`,
  `password_hash = NULL`, `user_name` is the `name` claim (may be empty), and `role` is `default_role`.
- With `require_approval` on the status is `pending_approval`, otherwise `active`. An account waiting for
  approval notifies administrators by the rules of registration, appears in Admin's existing approval list, and
  the browser returns to the sign-in page with `sso_error=pending`. The approval mechanism already exists and
  nothing new is built.
- Automatic creation is **not** subject to the public registration switch or the signup allow-list. An
  administrator who turns `auto_create` on for this IdP has made that decision. GitLab's
  `allow_single_sign_on` and `signup_enabled` are independent as well.
- It still obeys: no account before first-run setup is complete (503, as registration); `is_email_domain_blocked`;
  and the per-address rate limit shared with registration.
- **An email in `ADMIN_EMAILS` is never created automatically**; the result is `no_account`. The existing logic
  promotes an account whose email is on that list to administrator the next time a token is issued
  (`issue_account_token`). So an administrator account is first made the existing way and then connected to SSO
  through guided linking; one assertion from an IdP cannot produce it.

The defaults are cautious: automatic creation off, approval on. A deployment that wants "members sign in and
use it" sets this IdP to `auto_create: true` and `require_approval: false`, and the operator guide gives that
example.

## 9. Confirming sensitive actions

Today `confirm_account_owner` in `serving/servers/routers/reauth.py` accepts a password or a phone code only,
and an account with neither gets a 400. Three of the user's own actions call it: setting or changing the
password (`/user/change-password`), deleting the account (`/auth/delete-account`) and binding an email
(`/auth/email/bind`). An account that uses SSO only has neither a password nor a phone, so it can do none of
the three today.

How requests are authenticated decides the shape of the flow: the console's access token lives in the page's
memory and is sent in the `Authorization: Bearer` header, which a page navigation cannot carry. So the
confirmation is started by a `POST` with the Bearer header, the callback relies only on the flow row and the
flow cookie, and the next request with the Bearer header is what spends the confirmation. This record adds no
write endpoint authenticated by a cookie alone.

1. `GET /user/me` (which already returns `has_password`) gains `sso_reauth_provider`: when the account has no
   password, no verified phone, and is linked to an IdP that is still enabled, it gives that IdP's id and
   display name. When it is set, the confirmation dialog shows "Confirm with {name}" instead of a password
   field.
2. That button calls `POST /auth/sso/reauth/start` with the body `{"purpose": "...", "return_to": "..."}` and
   the Bearer header.
   - It uses the IdP the account is linked to; with several, the one most recently signed in with that is still
     enabled. Which IdP a link belongs to is decided as §11 says.
   - It inserts an `sso_flows` row that records the user id, the session id from the access token (`sid`) and
     the purpose.
   - It sets the flow cookie and returns `{"authorization_url": "..."}`. The authorization request also carries
     `prompt=login&max_age=0`, asking the IdP to have the user sign in again.
3. The callback shares its address with sign-in and is told apart by the purpose on the flow row. Besides every
   check of §6.1 it checks:
   - (issuer, sub) is the identity already linked to the row's `user_id`. Otherwise someone could confirm for
     you with their own IdP account.
   - `auth_time` is present and not earlier than 60 seconds before the flow started. This cannot be skipped:
     OIDC says an IdP only "should" honor `prompt=login`, while a request with `max_age` obliges the IdP to
     return `auth_time`.
4. When the checks pass, the confirmation time is recorded on the flow row, and the callback answers with a 302
   to `return_to` with `reauth=<purpose>` added.
5. The page sees that parameter, opens the same dialog again and shows "Identity confirmed". The user clicks
   confirm once more, and the request needs to carry nothing new. What was already typed before leaving (a new
   email, for example) is kept in this tab's `sessionStorage` and restored on return; a password is never kept.
   The page is loaded anew on return and gets a new access token, but the `sid` is the same within one session,
   so the confirmation still counts.
6. `confirm_account_owner` gains a third proof: when the account has no password and no verified phone but is
   linked to SSO, it looks for a row of the same user, the same `sid` and the same purpose that was confirmed
   within 5 minutes and is not used yet, and marks it used. The `sid` comes from the current request's access
   token. With no such row it returns 400 and tells the user to confirm at the IdP first.

The purposes are a closed table: `set_password`, `delete_account`, `bind_email`, and `link_sso` and
`unlink_sso` from §7.2. The order of precedence does not change: an account with a password still types it,
then the phone, and SSO last.

**An administrator's sensitive actions do not change and still accept a password only.** An account that uses
SSO only can become an administrator: another administrator can promote its role to `admin` in Admin. For an
administrator with no password the existing rule (`confirm_administrator`) refuses the action and says to set
a password in Settings first; administrators who registered by phone are already handled that way. SSO
accounts follow the same rule: `set_password` above lets them confirm at the IdP and set a password, and
administrator actions after that are confirmed with the password.

Most of the console work is here: the dialogs for deleting the account, binding an email and setting a
password each gain a "confirm at the IdP" branch and must return to the same step afterwards.

## 10. Other behavior

- **Account switcher**: in the account summaries `/auth/accounts` returns, an account with no password that is
  linked to an enabled IdP gains an `sso_provider` field. For such an account the sign-in page's `signInAgain`
  goes straight to `/auth/sso/{id}/start` instead of filling the email into the password form.
- **Signing out**: signs out of this site only and leaves the session at the IdP alone.
- **Deleting an account**: the existing soft delete does not change. After it, a sign-in with this identity gets
  `inactive`. When the account's row is hard-deleted, its links go with it through the foreign key.
- **Turning an IdP off**: an account linked to it that uses SSO only can no longer sign in, and can set a
  password with "forgot password". The existing "forgot password" does not require the account to have had a
  password, and a test pins that. This way out needs outgoing email to be configured; on a deployment without
  it, an administrator resets these accounts' passwords before turning the IdP off.
- **The IdP disables an account** (a member leaves the university or the company): the gateway is not told, and
  the account's existing sessions and API keys keep working. In v1 the operator suspends the account in Admin;
  automatic revocation is a follow-up pull request (§2).
- **An unverified registration holds its email**: the existing registration endpoint creates the account before
  the email is verified. When someone registers with a member's organization email and never verifies it, the
  account cannot sign in but keeps the address; the member's SSO sign-in then enters guided linking, and they
  must reset the password and verify the email before they can sign in to that account and complete the link.
  A suggested fix is to create the account only after verification. That changes the registration path, so it
  is not done here (§2).
- **Audit**: automatic creation records `create_user` (with the IdP's id), linking records `link_sso_identity`,
  and unlinking records `unlink_sso_identity`.

## 11. Data model

One expand-only migration, written like the other expand-only revisions (`op.execute`, `IF NOT EXISTS`,
`compatibility = "expand"`, with a migration test). Its number is the next free one when the pull request is
opened, `0019` now. The previous release never reads the new tables.

```sql
CREATE TABLE user_identities (          -- long-lived: which account here an IdP identity signs in to
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_id   TEXT NOT NULL,         -- the configuration id at the time of linking, for display only
    issuer        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    email         TEXT,                  -- the email at the time of linking, for display and audit only
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    UNIQUE (issuer, subject),
    UNIQUE (user_id, issuer)              -- one identity per IdP for an account
);
```

The other two tables are short-lived:

- `sso_flows`: one row per redirect, expiring after 10 minutes and usable once. It holds the hash of the state,
  the hash of the flow cookie, the IdP id, the purpose (`login`, `reauth` or `link`) and `return_to`. What the
  protocol needs meanwhile is one JSON column, for OIDC the hash of the nonce and the PKCE verifier, so another
  protocol changes no table. With the purpose `reauth` or `link` it also holds the user id and the `sid`;
  `reauth` adds what is being confirmed and two times, "confirmed" and "used".
- `sso_pending_links`: the data guided linking keeps meanwhile, expiring after 10 minutes. It holds the hash of
  the link cookie, the IdP id, issuer, sub and email.

Notes:

- Only hashes of the state and of the cookies are stored. The PKCE verifier is stored as it is: it is useful only
  together with the code the IdP issued to this callback address, the row lives 10 minutes at most, and it is
  used once.
- Expired rows are removed whenever a new row is inserted.
- A link is keyed by `issuer`, not `provider_id`: an administrator renaming an IdP does not affect existing
  links, while changing the issuer invalidates all of them, and the operator guide says so.
- Two IdPs may share an issuer: every Google Workspace organization signs in at `https://accounts.google.com`.
  So the issuer alone does not say which IdP a link belongs to. To show a link's IdP, or to confirm through it
  (§9), the gateway takes the IdP the link was made through (`provider_id`) while it is enabled and still has
  the link's issuer, and otherwise the first enabled IdP with that issuer.
- `provider_id` and `issuer` record which organization an account came from, which later serves usage figures
  by organization directly.

The storage methods are declared in `storage/base.py` and implemented in `postgres_operational.py`; the wrapper
in `cache.py` forwards each of them and caches nothing. A link is written with
`INSERT … ON CONFLICT DO NOTHING RETURNING`, and every "used once" is an `UPDATE … RETURNING`.

## 12. Where the code is

- **Backend, new**: `serving/auth/oidc.py` (the OIDC protocol itself, independent of FastAPI and the database);
  `serving/auth/sso_accounts.py` (the account resolution of §6.2, linking, account creation);
  `serving/config/sso.py` (the configuration document's model and reading it); `serving/storage/sso.py` (the
  storage methods, a mixin like the phone ones); `serving/servers/routers/sso_routes.py` (every endpoint in this
  record, registered in `app.py`); `serving/utils/sso_rate_limit.py`; one new migration.
- **Backend, changed**: `reauth.py` (the third proof); `utils/logging.py` (the access log leaves out a callback's
  query); `admin/users.py` (the links in the user detail, the unlink endpoint); `user_routes.py` and
  `auth_routes.py` (two new fields); `config/documents.py`, `app_config_registry.py` and `app_config.py` (the
  configuration document and its secret references); `site_config.py` (the `sso` block); `base.py`, `cache.py`
  and `postgres_operational.py` under `storage/`.
- **Console, new**: `lib/api/sso.ts` (calls to every endpoint in this record); `components/auth/ssoMessages.ts`
  (the string for each error code); `components/features/auth/SsoReauth.tsx` (confirming at the IdP and resuming
  after the return); `components/features/auth/SsoIdentities.tsx` (the SSO section in Settings).
- **Console, changed**: `app/login/page.tsx`, `site-ui/contract.ts`, `lib/i18n/messages/zh-CN.json`,
  `config/site-config.ts`, `lib/api/auth.ts`, `lib/api/client.ts`, `AuthProvider.tsx`, the three confirmations of
  §9 (deleting the account, binding an email, setting a password), the Settings page, and Admin's user detail
  panel (`admin/users/UserDetailPanel.tsx`).
- **Documentation**: a new `docs/developer/sso.md` for operators: how to configure it, testing locally with
  Keycloak, notes for kinds of IdP, when not to turn `auto_link` on, and suspending the account when a member
  leaves. It is written in English and added to `index.rst`, and its Chinese translation is updated in the same
  pull request. The glossary gains SSO, IdP and OIDC. `SSO_PROVIDERS` is added to the list of configuration
  documents in `configuration.md`, `architecture.md` and `AGENTS.md` §6.3.
- **Not changed**: `routing/`, the adapters, the `/v1` path, `deploy/public-paths.json`, the console's relay
  code, `distributions/`.

**Room for SAML and CAS.** The overall structure does not change when a protocol is added:

- `oidc.py` returns one protocol-neutral identity result: issuer, subject, email, whether the email is verified,
  name, and the time of the sign-in at the IdP. Account resolution, guided linking, account creation,
  confirmation and the sign-in page know only this result and no OIDC concept.
- `issuer` and `subject` in `user_identities` fit other protocols as well: the IdP's entityID and the NameID for
  SAML, the server address and the user name for CAS.
- Adding a protocol means three things: a new protocol module, a new value of `type` with its own configuration
  fields, and its own callback endpoint.
- No abstract base class or registry is written now for protocols that do not exist. The shared interface is
  drawn from the real differences when a second protocol is built. Two are known: a SAML callback is a
  cross-site POST, which a `SameSite=Lax` flow cookie does not accompany; and SAML and CAS have no
  `email_verified`, so they depend on `assume_email_verified`.

## 13. Tests

- **Unit** (`tests/unit/`): every validation case of `oidc.py` (against an in-process IdP that signs tokens with
  a real RSA key); validation of the configuration document (including that the example of §4.1 saves as it is)
  and domain matching; every row of §6.2 over an in-memory store; the third proof of confirmation.
- **Database** (`tests/integration/`, `make test-db-local`): the migration; concurrency and "used once" in the
  storage methods; the whole flow over HTTP, covering sign-in, account creation and approval, guided linking,
  confirmation, linking and unlinking in Settings, and unlinking by an administrator; and one test that pins
  "the gateway's own log contains no one-time value and no token".
- **A real IdP** (`tests/external/test_sso_keycloak.py`): sign-in, an unverified email, a wrong secret and
  confirmation against a Keycloak in local Docker. It is in the external tier, not in CI, and runs only when
  `SSO_KEYCLOAK_URL` is set.
- **Console**: the sign-in page with SSO on and off, the message when an email has no IdP, guided linking when
  signed in and when not, and a confirmation dialog resuming after the return.

## 14. Release and rollback

- Off by default. With no `SSO_PROVIDERS` or no enabled IdP, `/auth/sso/*` returns only `unavailable`, and
  site-config and the interface do not change.
- An older console ignores the top-level `sso` block, so the gateway and the console can be upgraded or rolled
  back in either order.
- The migration only adds tables. After a rollback to the previous release the tables sit unused and affect
  nothing.
- To withdraw SSO while running: set the IdP to `enabled: false`. Accounts that use SSO only set a password
  with "forgot password" and carry on (§10).

## 15. Delivery

The work is delivered as two pull requests that change no file in common, so both can be open at once and each
shows only its own half:

1. **Gateway**: the migration and storage, `oidc.py`, the configuration document, every `/auth/sso/*` endpoint,
   account resolution, confirmation, the administrator's unlink endpoint, their tests, the operator guide and
   this record.
2. **Console**: the sign-in page, the SSO section in Settings, the three confirmation dialogs, Admin's user
   detail panel, their tests and the Chinese strings.

The gateway merges first. Between the two merges nothing a user sees changes: the console shows the SSO entry
only when `/site-config` reports `sso.enabled`, and a console built before the second pull request ignores that
block (§4.4). The feature is usable once both are merged. Neither half is split further, because a smaller
step would not be complete: without guided linking a user who already has an account cannot use SSO, and
without confirmation an account that uses SSO only cannot take a sensitive action.

The commits of each pull request are in reading order. Gateway:

1. The migration and the storage methods
2. `oidc.py`
3. The configuration document, the sign-in endpoints, account resolution, account creation, site-config
4. Confirmation, linking and unlinking for the user, viewing and unlinking for an administrator
5. Tests against a real Keycloak
6. The operator guide and this record
7. Additions after the walk-through: the access-log filter, where SSO is configured and when its secret
   applies, and refusing to link an identity whose email is another account's

Console:

1. Single sign-on in the console: the sign-in page, Settings, the confirmation dialogs, Admin's user panel
2. Additions after the walk-through: the message for an account's only sign-in identity, the entry under a
   distribution's skin, and the message for an identity whose email is another account's

Checks run on the final code, each pull request's branch on its own: on the gateway branch `make lint`,
`make test`, `make test-db-local` (the whole `dbtest` tier) and `make docs-verify`; on the console branch the
console's format, lint, type-check and test gates. With the two together:
`tests/external/test_sso_keycloak.py` against a local Keycloak, and the gateway run from source behind the
console, where sign-in, account creation, guided linking, confirmation, linking and unlinking in Settings, and
unlinking by an administrator were walked through in a real browser. The example deployment was also built from
the two together and passed `make up`, `make smoke`, `make demo` and `make demo-smoke` (all with
`DISTRIBUTION=example`).

## 16. Alternatives considered

| Question | Not chosen | Why |
|---|---|---|
| Where SSO is done | Not in the gateway; the operator puts an authenticating proxy in front | Accounts, API keys and quotas are in the gateway. A proxy can only block access; it cannot map an organization's identity to a gateway account |
| Where the IdP configuration is stored | A separate table with an Admin form | Configuration documents are what the repository already does, with validation, audit, backup and administrator confirmation in place. A form can be added on top of the document later |
| Where one redirect's temporary state is kept | A signed or encrypted cookie | Only the database guarantees "used once", it is consistent across processes, and no new key is introduced |
| What implements the OIDC protocol | A library such as Authlib | The part of the protocol in use is small, and `httpx` and `PyJWT` are already dependencies; no new dependency is needed |
| The form of the sign-in entry | One button per IdP, or buttons together with "email first" | One "Log in with SSO" entry only: the page does not list the connected organizations, the flow is the same however many IdPs there are, and it is what common products do. The cost is that a deployment with a single IdP still asks for the email once |
| What happens to an existing account | Merge by default when the emails match | Matching emails do not prove it is the same person (§6.2). Guided linking is the default, and automatic linking is an option with conditions |
| How the console learns SSO is on | A key in `features` | `features` is parsed strictly, and one extra key makes an older console reject the whole document |
| How an account that uses SSO only confirms an action | Require such an account to set a password first | Setting the first password itself needs confirmation, which such an account cannot give |
| Whether an administrator's sensitive actions accept SSO confirmation | Accept it | The existing rule is that an administrator with no password sets one first. An SSO account can do that too, and keeping the rule leaves "administrators confirm with a password only" as it is |

## 17. Open question for review

The Site UI contract: can the approach of §5.1 (existing classes only, new `data-auth` markers, keys added to
`AUTH_MESSAGE_KEYS`) stay within the current contract version?

## 18. References

- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html): the authorization code
  flow, ID token validation, `prompt`, `max_age`, `auth_time`
- [OpenID Connect Discovery 1.0](https://openid.net/specs/openid-connect-discovery-1_0.html)
- [RFC 7636 PKCE](https://www.rfc-editor.org/rfc/rfc7636)
- [GitLab OmniAuth](https://docs.gitlab.com/integration/omniauth/): `allow_single_sign_on`,
  `block_auto_created_users`, `auto_link_user`, and guided linking
