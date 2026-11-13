# Single Sign-On

This page is for an operator who wants the members of an organization, such as
a university or a company, to sign in to the console with the organization's
own accounts. After reading it you can connect an identity provider, decide
what a member's first sign-in does, and know what the gateway does not do when
someone leaves the organization.

The gateway supports identity providers that speak OpenID Connect. SAML and CAS
are not supported yet.

## Connect a provider

1. **Register the gateway at the identity provider** as a confidential client
   that uses the authorization code flow. Set its redirect URI to
   `<FRONTEND_URL>/auth/sso/<id>/callback`, where `<id>` is the short name you
   will give the provider below. Note the client id and the client secret.
2. **Store the client secret** in **Admin > Configuration** as a secret setting,
   for example `SSO_EXAMPLE_UNIV_CLIENT_SECRET`.
3. **Describe the provider** in the `SSO_PROVIDERS` setting, on the same tab:

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

4. **Check `FRONTEND_URL`.** It must be the address users open the console at.
   The redirect URI is built from it, and users must reach the console through
   that address for a sign-in to complete.

The change applies without a restart. The sign-in page now shows **Log in with
SSO**. A user selects it, enters their work or school email, and is sent to the
provider whose `domains` cover that email. The page does not list which
organizations are connected.

Saving the setting asks for the administrator's password, as other changes to
who may sign in do.

## Provider fields

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Short name: lower-case letters, digits and hyphens. It is part of the redirect URI, so changing it means changing the redirect URI at the provider too |
| `type` | required | The protocol. Only `oidc` is accepted |
| `display_name` | required | The name users see when they link or confirm with this provider |
| `enabled` | `true` | Set `false` to stop offering the provider. Existing links are kept |
| `issuer` | required | The provider's issuer URL, exactly as its discovery document states it. It must use `https`; `http` is accepted for `localhost` only |
| `client_id`, `client_secret` | required | `client_secret` is always a `${NAME}` reference to a secret setting, never the secret itself |
| `client_auth_method` | `client_secret_basic` | How the gateway authenticates to the token endpoint. The other value is `client_secret_post` |
| `scopes` | `["openid", "email", "profile"]` | Must include `openid` |
| `domains` | required | The email domains this provider answers for. `example.edu` matches that domain; `*.example.edu` matches its subdomains and not `example.edu` itself. Two providers cannot claim the same domain |
| `assume_email_verified` | `false` | Treat the provider's email as verified although the provider does not say so |
| `auto_create` | `false` | Create an account at a member's first sign-in |
| `require_approval` | `true` | Put an account created that way in the approval queue |
| `auto_link` | `false` | Link an existing account that has the same email without asking its owner |
| `default_role` | `free` | Role of an account created at first sign-in: `free`, `pro` or `internal` |

## What a sign-in does

The gateway identifies a member by the provider's own identifier for them, not
by their email. Once that identifier is linked to an account, the member signs
in to that account, whatever email the provider reports later.

For an identifier that is not linked yet, the gateway uses the email, and only
when both of these hold:

- the email is in one of the provider's `domains`;
- the provider reports the email as verified, or `assume_email_verified` is on.

Otherwise the sign-in is refused. A provider vouches only for its own domains.

With a trusted email, the result depends on whether an account already has it:

| Account with that email | Result |
|---|---|
| None, and `auto_create` is off | The sign-in is refused |
| None, and `auto_create` is on | An account is created with `default_role`. With `require_approval` it waits in **Admin > Users** like any registration |
| Exists and is active | The member is asked to sign in to it once, the way they usually do, and then to accept the link |
| Exists and is not active | The sign-in is refused. This includes a deleted account: its email stays taken, as it does for signup |

Creating accounts this way does not depend on the public signup switch or the
signup allowlist. An address on `ADMIN_EMAILS` is never created this way.

### Let members in without approval

To give every member an account that works at their first sign-in:

```json
"auto_create": true,
"require_approval": false
```

### Link existing accounts without asking

With `auto_link` on, an active account that has the same email is linked at
once, when the deployment requires email verification, the account's email is
verified and the account is not an administrator. Leave it off in two cases:

- **The deployment has ever run without email verification.** Accounts
  registered during that time are marked verified without proof, so someone
  could have registered a member's address.
- **The organization reassigns email addresses.** A new member who receives an
  old address would be linked to the previous holder's account.

### Providers that do not report a verified email

Some providers never send `email_verified`. Microsoft Entra ID is one. If the
organization manages its own email addresses, set `assume_email_verified` to
`true` for that provider. Without it, no member of such a provider can complete
a first sign-in.

## Linking and unlinking

A user manages their own links under **Settings > Single sign-on**: they can
connect an organization account, including one whose email differs from the
account's, and disconnect one. Both ask the user to confirm, and the only way
to sign in to an account cannot be disconnected.

An administrator sees a user's links in the user's detail panel under **Admin >
Users** and can unlink one. Unlinking removes the link only: it does not end
the account's sessions or revoke its API keys. Administrators cannot create a
link for a user.

If a sign-in is refused because the account is already connected to a different
identity from the same organization, the provider has recreated the member or
given the address to someone else. Confirm which, then unlink the old identity.

## Confirming sensitive actions

An account created by single sign-on has no password. When its owner deletes
the account, sets a password or changes the sign-in email, the console sends
them to the provider to sign in again instead of asking for a password.

An administrator's own confirmations always ask for a password. An account
that signs in only through a provider and is made an administrator sets a
password first, under **Settings**.

## When a member leaves the organization

The gateway is not told when a provider disables an account. The member can no
longer sign in through the provider, but sessions that are already open and
API keys keep working. Suspend the account in **Admin > Users** to end both.

## Stop offering a provider

Set `enabled` to `false`. Accounts that sign in only through that provider can
then use **Forgot password** to set a password, which needs outgoing email to
be configured. Without outgoing email, set passwords for those accounts before
you disable the provider.

Changing a provider's `issuer` breaks every existing link to it, because links
are recorded against the issuer.

## Try it with a local provider

You can test against [Keycloak](https://www.keycloak.org/) on your own
machine. Run the gateway from a
[development checkout](installation.md#development-checkout-no-docker) rather
than in a container, so that `localhost` names the same machine for your
browser and for the gateway.

1. Start Keycloak on a port the gateway does not use:

   ```bash
   docker run --rm -p 8081:8080 \
     -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
     quay.io/keycloak/keycloak:26.0 start-dev
   ```

2. In the Keycloak admin console at `http://localhost:8081`, create a realm,
   a user with an email and a password, and an OpenID Connect client with
   client authentication on and the redirect URI
   `<FRONTEND_URL>/auth/sso/keycloak/callback`.
3. In the gateway, store the client's secret as a secret setting and add a
   provider whose `issuer` is `http://localhost:8081/realms/<realm>`, whose
   `id` is `keycloak` and whose `domains` include the domain of the user's
   email.
4. Open the console's sign-in page, select **Log in with SSO** and enter the
   user's email.

Mark the user's email as verified in Keycloak, or the sign-in is refused.

## When a sign-in is refused

The sign-in page shows a short message. The gateway's log names the provider
and the reason, and never contains a token or a secret.

| Message says | Usual cause |
|---|---|
| Single sign-on is unavailable | The provider cannot be reached, the client secret setting is empty, or `FRONTEND_URL` is not an absolute URL |
| The sign-in took too long | The user took more than ten minutes, opened the callback in another browser, or reached the console through an address other than `FRONTEND_URL` |
| The email could not be confirmed | The email is outside the provider's `domains`, or the provider does not report it as verified |
| No account here yet | `auto_create` is off, or the address is on `ADMIN_EMAILS` |
| Pending approval | The account was created and waits in **Admin > Users** |
| Already connected to a different identity | See [Linking and unlinking](#linking-and-unlinking) |
