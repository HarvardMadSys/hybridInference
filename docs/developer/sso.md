# Single Sign-On

This page is for an operator who wants an organization's members to sign in to
the console with the organization's own accounts. The gateway supports OpenID
Connect identity providers; SAML and CAS are not supported.

## Connect a provider

1. **Register the gateway at the identity provider** as a confidential client
   using the authorization code flow, with the redirect URI
   `<FRONTEND_URL>/auth/sso/<id>/callback`. `<id>` is the short name you give
   the provider in the next step. Note the client id and secret.
2. **Describe the provider** in the `SSO_PROVIDERS` setting, under
   **Admin → Settings → Sign-in & security → Advanced settings**:

   ```json
   {
     "providers": [{
       "id": "example-univ",
       "type": "oidc",
       "display_name": "Example University",
       "issuer": "https://idp.example.edu/realms/main",
       "client_id": "hybridinference",
       "client_secret": "${SSO_EXAMPLE_UNIV_CLIENT_SECRET}",
       "domains": ["example.edu", "*.example.edu"]
     }]
   }
   ```

   `client_secret` names a secret setting, never the secret itself.
3. **Store the client secret.** Saving the provider adds the setting it names,
   here `SSO_EXAMPLE_UNIV_CLIENT_SECRET`, under
   **Admin → Settings → Sign-in & security**, in the **Security** section.
   Enter the secret there.
4. **Restart the gateway.** It reads the secret at startup.
5. **Check `FRONTEND_URL`.** It must be the address users open the console at:
   the redirect URI is built from it, and a sign-in completes only through that
   address.
6. **Check which host the console calls the gateway on.** The sign-in's cookies
   must reach both that host and the host of `FRONTEND_URL`. A console built
   with an empty `NEXT_PUBLIC_API_BASE` calls its own host, and nothing more is
   needed. When `NEXT_PUBLIC_API_BASE` names another host, set `COOKIE_DOMAIN`
   to a domain that covers both. Two ports on `localhost` are one host for
   cookies.

The sign-in page shows **Log in with SSO** once the provider is saved; until the
secret is stored and the gateway restarted, it reports that single sign-on is
unavailable. A user enters their work or school email and is sent to the
provider whose `domains` cover it. The page does not list connected
organizations.

Later changes to `SSO_PROVIDERS` apply without a restart; a changed client
secret needs one. Saving either asks the administrator to confirm who they are
(see [Confirming sensitive actions](#confirming-sensitive-actions)).

## Provider fields

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Short name: lower-case letters, digits and hyphens. It is part of the redirect URI, so changing it means changing the redirect URI at the provider too. Keep the provider's `domains` when you change its `id`, and its links follow it |
| `type` | required | The protocol. Only `oidc` is accepted |
| `display_name` | required | The name users see when they link or confirm with this provider |
| `enabled` | `true` | Set `false` to stop offering the provider. Existing links are kept |
| `issuer` | required | The provider's issuer URL, exactly as its discovery document states it. It must use `https`; `http` is accepted for `localhost` only |
| `client_id`, `client_secret` | required | `client_secret` is always a `${NAME}` reference to a secret setting, never the secret itself |
| `client_auth_method` | `client_secret_basic` | How the gateway authenticates to the token endpoint. The other value is `client_secret_post` |
| `scopes` | `["openid", "email", "profile"]` | Must include `openid` |
| `required_claims` | `{}` | Claims the ID token must carry, each with exactly the value given. See [Issuers that several organizations share](#issuers-that-several-organizations-share) |
| `domains` | required | The email domains this provider answers for. `example.edu` matches that domain; `*.example.edu` matches its subdomains and not `example.edu` itself. Two providers cannot claim the same domain |
| `assume_email_verified` | `false` | Treat the provider's email as verified although the provider does not say so |
| `auto_create` | `false` | Create an account at a member's first sign-in |
| `require_approval` | `true` | Put an account created that way in the approval queue |
| `auto_link` | `false` | Link an existing account that has the same email without asking its owner |
| `default_role` | `free` | Role of an account created at first sign-in: `free`, `pro` or `internal` |

## What a sign-in does

The gateway identifies a member by the provider's identifier for them, not by
email. Once that identifier is linked to an account, the member signs in to it
whatever email the provider reports later. For an identifier not linked yet,
the gateway trusts the email only if it is in one of the provider's `domains`
and the provider reports it verified (or `assume_email_verified` is on);
otherwise the sign-in is refused. Then:

| Account with that email | Result |
|---|---|
| None, and `auto_create` is off | The sign-in is refused |
| None, and `auto_create` is on | An account is created with `default_role`. With `require_approval` it waits in **Admin → Users** like any registration |
| Exists and is active | The member signs in to it once, the usual way, and accepts the link |
| Exists and is not active | The sign-in is refused. A deleted account's email stays taken, as for signup |

Creating accounts this way ignores the public signup switch and the signup
allowlist, but never creates an account for an address on `ADMIN_EMAILS`. To
let every member in at their first sign-in, set `"auto_create": true` and
`"require_approval": false`.

### Link existing accounts without asking

With `auto_link` on, an active account with the same email is linked at once
when the deployment requires email verification, the account's email is
verified, and the account is not an administrator. Leave it off if the
deployment has ever run without email verification (accounts registered then
were marked verified without proof), or if the organization reassigns email
addresses (a new member would get the previous holder's account).

### Providers that do not report a verified email

Some providers, Microsoft Entra ID among them, never send `email_verified`. If
the organization manages its own addresses, set `assume_email_verified` to
`true` for that provider; otherwise no member can complete a first sign-in.

### Issuers that several organizations share

Some providers sign in every organization's accounts at one issuer. At Google,
an organization account and a personal account both come from
`https://accounts.google.com`, and a personal account can carry a verified
email in your domain. Require the claim that names the organization, and a
sign-in whose ID token lacks it is refused:

```json
"required_claims": {"hd": "example.edu"}
```

For Google the claim is `hd`, the organization's primary domain. A member signs
in only through the provider they were linked through, so a provider's
`required_claims` and `enabled` switch hold for all of its members.

## Linking and unlinking

Users connect and disconnect organization accounts under **Settings → Single
sign-on**; both ask them to confirm, and the only way to sign in to an account
cannot be disconnected. The organization account's email may differ from the
account's, but it cannot be another account's email: the user signs in to that
account and connects there.

An administrator sees a user's links in the user's detail panel under **Admin →
Users** and can unlink one, but cannot create one. Unlinking removes the link
only; it does not end sessions or revoke API keys. A sign-in refused because
the account is linked to a different identity from the same organization means
the provider recreated the member or gave the address to someone else; confirm
which, then unlink the old identity.

## Confirming sensitive actions

An account created by single sign-on has no password. When its owner deletes
the account, sets a password or changes the sign-in email, the console sends
them to the provider to sign in again.

An administrator confirms a security-relevant change in **Admin** with a code
from the account's authenticator app, or with the account's password. An
administrator who signs in only through a provider, with no password and no
verified phone, signs in at the provider again instead; each sign-in confirms
one change in the same session within five minutes. An account with a verified
phone but no password must set a password under **Settings** first, and adding
an authenticator app also needs a password.

## When a member leaves the organization

The gateway is not told when a provider disables an account. The member can no
longer sign in through the provider, but open sessions and API keys keep
working. Suspend the account in **Admin → Users** to end both.

## Stop offering a provider

Set `enabled` to `false`. Accounts that sign in only through that provider can
then set a password with **Forgot password**, which needs outgoing email;
without it, set their passwords before you disable the provider. Removing the
provider from `SSO_PROVIDERS` keeps its members out too, and their links stay.
A provider with the same issuer takes over a link only when its `domains` cover
the email the link was made with. Changing a provider's `issuer` breaks every
existing link to it, because links are recorded against the issuer.

## Try it with a local provider

Run the gateway from a
[development checkout](installation.md#development-checkout-no-docker), so that
`localhost` means the same machine to your browser and the gateway, and start
[Keycloak](https://www.keycloak.org/) on another port:

```bash
docker run --rm -p 8081:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.0 start-dev
```

In the Keycloak admin console at `http://localhost:8081`, create a realm, a user
with a verified email and a password, and an OpenID Connect client with client
authentication on and the redirect URI `<FRONTEND_URL>/auth/sso/keycloak/callback`.
Then add a provider with `id` `keycloak`, `issuer`
`http://localhost:8081/realms/<realm>` and `domains` covering the user's email,
store the client secret, restart the gateway, and sign in with **Log in with
SSO**.

## When a sign-in is refused

The sign-in page shows a short message. The gateway's log names the provider and
the reason, and never contains a token or a secret.

| Message says | Usual cause |
|---|---|
| Single sign-on is unavailable | The provider cannot be reached, the client secret setting is empty, the gateway has not restarted since the secret was stored, or `FRONTEND_URL` is not an absolute URL |
| The sign-in took too long | The user took more than ten minutes, opened the callback in another browser, or reached the console through an address other than `FRONTEND_URL`. Every sign-in ends this way when the console calls the gateway on another host and `COOKIE_DOMAIN` does not cover both: see step 6 of [Connect a provider](#connect-a-provider). A link from **Settings** also ends this way when the user signed out before it completed |
| Too many sign-in attempts | More than 120 sign-ins were started from one client address in an hour. Behind a reverse proxy the gateway sees the proxy's address for every browser, and all of them share that allowance, until it is told which proxies to trust: see [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md) |
| Single sign-on failed | The provider's answer did not validate, the account lacks a claim that `required_claims` asks for, or the member was linked through another provider that has the same issuer |
| The email could not be confirmed | The email is outside the provider's `domains`, or the provider does not report it as verified |
| No account here yet | `auto_create` is off, or the address is on `ADMIN_EMAILS` |
| Pending approval | The account was created and waits in **Admin → Users** |
| Already connected to a different identity | See [Linking and unlinking](#linking-and-unlinking) |
