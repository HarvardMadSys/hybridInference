# API Key Authorization

This page is for an operator who wants to let an application, such as an agent
or a chat client, create an API key on a user's own account, and for the
developer of that application. After reading it you can turn the feature on
for an application and implement the three steps the application takes.

The user approves the application in the console, signing in or creating an
account on the way, and the application receives an ordinary API key on that
user's account. The key spends the account's quota, and the user can revoke it
like any other key. The flow is OAuth 2.0 with PKCE, with the parameters and the
response of OpenRouter's PKCE key flow and `S256` as the only challenge method.

## Turn it on for an application

1. Ask the application for its callback URL: the address on its server that
   the user's browser returns to, such as
   `https://app.example.org/connections/callback`.
2. Add it to the `API_KEY_AUTHORIZATION_CALLBACKS` setting, under
   **Admin → Settings → Integrations**. The setting is a comma-separated list
   of callback URLs, one or more for each application.

Changes apply without a restart. Saving the setting asks the administrator to
confirm, as other security settings do. An empty list turns the feature off:
the consent page and every endpoint refuse.

A callback is matched on its scheme, host, port and path, which must equal an
entry exactly. Its query string belongs to the application and is not
compared. Each entry must:

- use `https`, or `http` for `localhost` and `127.0.0.1` only;
- have no query string, fragment or user information;
- name its host with letters, digits, hyphens and dots.

## What the user sees

The application sends the browser to the console's consent page,
`<FRONTEND_URL>/authorize/key`. The page names the application by its
callback's host, never by a name the application supplies, and says that the
application will hold an API key on the user's account. The user chooses
**Allow** or **Cancel**.

- A user who is not signed in goes through the sign-in page and comes back. A
  new user can create an account on the way.
- The account must be active and its email address verified. A user whose
  address is not verified is asked to verify it on the page.
- With several accounts signed in on the browser, the user picks one.

The key is created in the account's Default project, named after the
callback's host, with a note that API key authorization created it. It is
listed on the user's API keys page, where it can be revoked.

## Implement the application

### Send the user to the consent page

Make a random code verifier and keep it in the user's session: 43 to 128
characters of letters, digits, `-`, `.`, `_` and `~`. The code challenge is the
SHA-256 of the verifier, base64url-encoded without padding, which is 43
characters long. Then send the browser to:

```text
<FRONTEND_URL>/authorize/key?callback_url=<callback>&code_challenge=<challenge>&code_challenge_method=S256
```

URL-encode the callback. Put your own state, such as a CSRF token, in the
callback's query string: it comes back unchanged. The callback must not carry
a `code` or `error` parameter of its own.

### Receive the answer

The browser returns to the callback with one parameter appended to its query
string:

- `code=<code>` when the user allowed the request;
- `error=access_denied` when the user cancelled.

The code can be exchanged once, within three minutes.

### Exchange the code for the key

From the application's server:

```text
POST /v1/auth/keys
Content-Type: application/json

{"code": "<code>", "code_verifier": "<verifier>", "code_challenge_method": "S256"}
```

A `200` answers `{"key": "<API key>", "user_id": "<account id>"}`. The key is
returned this once, so store it. The request carries no credential of the
application's: the verifier proves it started the flow. It works on the gateway
directly and through the console's `/v1` path.

Errors are JSON in OAuth's shape, `{"error": "...", "error_description": "..."}`:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_request` | The body is malformed: no code, a verifier of the wrong length or characters, or a method other than `S256` |
| 400 | `invalid_grant` | The code is unknown, expired or already used, the verifier does not match, or the callback was taken off the list. Start again from the consent page |
| 403 | `access_denied` | The account may no longer create keys, for example because it was suspended |
| 404 | `not_configured` | This gateway does not offer API key authorization |
| 409 | `key_limit_reached` | The account already holds the most active API keys it may |
| 429 | `rate_limited` | Too many failed exchanges came from this address. Wait as long as `Retry-After` says |

Each code gets one exchange attempt, whatever its outcome.

## Security

- A code is random, stored only as a hash, valid for three minutes and spent
  by its first exchange attempt.
- PKCE binds a code to the application that started the flow. A code read from
  a log or a browser history cannot be exchanged without the verifier.
- Only a listed callback receives a code or a cancellation. The consent page
  checks the request with the gateway before it shows anything, and sends the
  browser only to an address the gateway built.
- The key belongs to the user's own account and spends its quota. Revoking it
  on the API keys page cuts the application off.
- Failed exchanges are limited for each client address.
