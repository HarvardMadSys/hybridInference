# A second factor for administrators (2026-11-15)

Status: implemented, backend and console.
Code: `apps/backend/serving/auth/two_factor.py` (codes, policy),
`apps/backend/serving/storage/two_factor.py` (rows),
`apps/backend/serving/servers/routers/two_factor_routes.py` (the account's
routes), `apps/backend/serving/servers/deps.py` (the administrator check),
`apps/backend/serving/servers/routers/admin/reauthentication.py` (changes that
outlast the session), migration `0019_two_factor`.

## Problem

A console session is a bearer credential. Whoever holds a copy of it -- from
the browser profile it lives in, from a machine or an automation tool that was
signed in, from a request pasted somewhere with its headers -- is the account
for as long as the session lasts, which is thirty days and renews with use.
For an administrator account that is the whole deployment: approve an account,
mint it a key, raise it to `admin`, open reserved models to everyone.

What the gateway asks of a client on the Admin API is what a copied session
has:

- a signed access token naming a live session of an account whose role is
  `admin` (`admit_admin_session`);
- for the changes that outlast the session (raising a role, minting a key for
  another account, exporting secrets), the administrator's password (#1756).
  A password is also something that is copied, often from the same place as
  the session. Changing who may use a model asked for nothing more.

Nobody has to sign in for any of this, so nothing a sign-in could check is
checked, and the audit log is the only place it shows.

There are many ways to obtain a session and the gateway controls few of them.
The design therefore does not depend on closing one way in: it asks for
something a copied session, and a copied password, do not contain.

## Decision

An account can add an **authenticator app** (TOTP, RFC 6238). With
`ADMIN_TWO_FACTOR_REQUIRED=true`, a console session has administrator access
only after it has given a code from that app, and only from the kind of client
it gave the code from. Changes that outlast the session ask for a code given in
the last few minutes.

TOTP rather than a vendor's push service: it is an open standard every
authenticator app reads (Duo Mobile included), needs no outside service or
per-seat licence, and this gateway is deployed by people who have neither.
SMS is not offered for this: it is the weaker factor and depends on a provider.
Passkeys are the stronger one and are left for later; nothing here prevents
adding them as a second kind of proof for the same session flag.

### What is stored

| Where | What |
|---|---|
| `user_two_factor` | One row per account: `secret_encrypted`, `confirmed_at`, `last_used_step`, `failed_attempts`, `locked_until`; and a setup in progress: `pending_secret_encrypted`, `pending_session_id`, `pending_expires_at`, `pending_attempts_left` |
| `two_factor_recovery_codes` | SHA-256 of each one-time code, and `used_at` |
| `auth_sessions.two_factor_verified_at`, `.two_factor_environment` | When this session last gave a code, and from what kind of client |

The shared secret is encrypted with a key derived from `API_KEY_SECRET` under
its own label (it cannot be hashed: the gateway needs it to compute the code).
A pending secret becomes the account's only when its first code is given, so
replacing a device never leaves the account without a working one.
`last_used_step` makes a code good once.

Accepting a code and recording what the session proved are one transaction
(`verify_session_two_factor`), under the row lock of `user_two_factor` that
every change of authenticator also takes. Done as two steps, a request could
have its code for the old device accepted, lose the processor while the device
was replaced and every session's proof forgotten, and then mark its own session
verified anyway. For the same reason the transaction is told which stored
secret the code was checked against, and accepts nothing if the row now holds
another.

The migration is expand-only. The previous release reads neither table and
names its `auth_sessions` columns in every statement.

### Three questions, one flag

Everything is decided from what the session has proved
(`SessionTwoFactor.refusal`):

| Asked by | Requires | Otherwise |
|---|---|---|
| Every `/admin` route, when the deployment requires it | the account has an authenticator; this session gave a code; from this environment | 403 `TWO_FACTOR_ENROLLMENT_REQUIRED` or `TWO_FACTOR_REQUIRED` |
| A change that outlasts the session, for an account with an authenticator | the same, and within `TWO_FACTOR_STEP_UP_MINUTES` (default 10) | 403 `TWO_FACTOR_REQUIRED`, `reason: stale` |
| The same change, for an account without one | the password, as before | 400 |

The console answers `TWO_FACTOR_REQUIRED` by asking for six digits, calling
`POST /auth/two-factor/verify`, and retrying. There is no code field on any
other request.

### The console

Code: `apps/frontend/src/components/features/twoFactor/`,
`apps/frontend/src/components/providers/TwoFactorProvider.tsx`, and the
retry in `fetchWithAuth` (`apps/frontend/src/lib/api/client.ts`).

- **One place answers the refusal.** `fetchWithAuth` sees a 403
  `TWO_FACTOR_REQUIRED`, asks the installed prompt for a code, and sends the
  request once more. Every admin page gets this without knowing about it, and
  a model-visibility change (which has no confirmation of its own) is covered
  the same way as a role change. Requests refused together share one dialog.
  A request to `/auth/two-factor/verify` is never asked about, and a request
  made for one session is not retried as another.
- **Administration is not mounted for a session that owes a code.** The admin
  layout reads `GET /auth/two-factor` first and shows the code form, or the way
  to Settings for an account with no authenticator, instead of pages that would
  load into refusals. If the status cannot be read (a gateway older than the
  console), the pages open as before and the gateway decides.
- **The password field gives way to the code.** A confirmation that asked for
  the administrator's password says a code confirms it when the account has an
  authenticator, and sends no password.
- **The Settings section is an administrator's.** Sign-in does not ask for a
  code, so showing every user a "two-factor authentication" switch would
  promise a protection their account does not get. Other accounts see the
  section once they have an authenticator, or at
  `/dashboard/settings#two-factor`: an account must add one before it can be
  made an administrator, and the refusal of that promotion names the page.
- The status is read only on the pages that need it (Settings,
  administration), never on the rest of the console.

Changes that outlast the session: raising a role, minting a key for another
account, exporting secret values, restoring or changing security settings,
removing another account's authenticator, and (new) changing who may use a
model. The last asks nothing of an account without an authenticator, so
existing consoles keep working until an administrator enrolls.

### Guessing

Three of a million codes are valid at any moment, so six digits are as strong
as the number of guesses allowed at them. The sign-in limiter is the wrong tool
twice over: it counts successes, so an administrator's own codes would use up
their sign-in attempts, and it is per process and mostly per address, which a
holder of a copied session sidesteps by changing address.

Wrong codes are counted on the account's row instead. Every attempt is counted
before the code is looked at, in the statement that decides whether it may be
looked at, so of any number sent at once five are checked. The fifth wrong code
in a row holds the account's codes off for fifteen minutes, the tenth for
thirty, doubling to a day. An accepted code ends the count. Over a thirty-day
session that is about 180 guesses, one chance in two thousand.

Someone holding a session can therefore keep the owner's authenticator held
off. A right recovery code is accepted during a hold and ends it: fifty bits
cannot be guessed, so there is nothing to protect them from.

The first code of a new authenticator is a guess too, and a more valuable one:
whoever gives it receives the recovery codes and a verified session, and the
old device stops working. An earlier draft of this design left it unlimited on
the grounds that only the person shown the secret would be confirming it. That
is not so. The pending secret belongs to the account, so any other holder of a
session of it could have guessed at a setup the owner started, for as long as
it was left waiting. A setup therefore belongs to the session that started it
(`pending_session_id`), waits ten minutes (`SETUP_SECONDS`), and has five tries
(`SETUP_ATTEMPTS`), each taken before the code is looked at. After that it is
started again, which costs the password or a fresh code. Cancelling in the
console drops it (`DELETE /auth/two-factor/setup`). A copy of the very session
that started the setup shares its five tries and no more: fifteen chances in a
million per setup.

### Environment

`client_environment` reduces the `User-Agent` to a client family and a platform
family (`chrome/macos`, `curl/other`), without versions. A session verified in
one environment is unverified in another, and verifying there moves it.

This is what refuses a browser's session presented by a script. It is a check
on a header the caller writes: someone who copies the session and the header
passes it. It is one layer, kept because
it is cheap and its false positives are rare (a browser updating itself is the
same environment). Network location is not part of it: the request path has no
ASN data, country data is optional, and address changes are too common to ask
for a code on.

Signing in does not ask for a code. A session without one is an ordinary
account's session: it reaches the account's own pages and nothing under
`/admin`. Keeping the sign-in path untouched is what lets this ship without
changing login, phone login, account switching and first-run setup at once.

### Who may change the second factor

| Change | Proof |
|---|---|
| Add the first authenticator | The account's password. A copied session must not add its own device. |
| Replace it, remove it, new recovery codes | A code from the current one, within the window |
| Remove another account's (lost device) | An administrator's own recent code; refused for `ADMIN_TOKEN` and for one's own account |

Where a second factor is required, an administrator cannot remove their own
(the account would have no administrator access left to add one with), and an
account is raised to `admin` only once it has one.

### The two ways around it

- **`ADMIN_TOKEN`** is one fixed string with no account behind it. While
  `ADMIN_TWO_FACTOR_REQUIRED` is on it is answered for `GET`/`HEAD`/`OPTIONS`
  only. A deployment whose automation writes with the token has to move that
  automation before turning the setting on.
- **`ADMIN_EMAILS`** still gives a listed address the `admin` role at sign-in
  and at start. The role is no longer enough: that account has no administrator
  access until it has an authenticator and its session has given a code.

Both settings are read from the environment only. A session that holds
administrator access cannot switch its own second factor off.

## Rollout

1. Deploy. Nothing changes: the setting is off, and no account has an
   authenticator.
2. Administrators add an authenticator in Settings. **Admin → Users** shows
   `Two-factor: on` in the details of each account that has.
3. Set `ADMIN_TWO_FACTOR_REQUIRED=true` and restart. An administrator who has
   not enrolled is sent to do so on their next visit.

Break-glass, for a deployment whose only administrator lost both the device and
the recovery codes: `DELETE FROM user_two_factor WHERE user_id = ...` on the
database, after which the account adds a new authenticator with its password.

## What this does not do

- **The enrollment gap.** Until an administrator account has an authenticator,
  someone holding its password can add their own. Enroll before, not after,
  turning the requirement on, and treat a `two_factor.enable` audit row nobody
  expected as an incident.
- **An administrator acting in bad faith**, or an account an attacker has
  already been made administrator of, adds its own device like anyone else.
  What stops a copied session from creating such an account is that raising it
  needs the first administrator's code.
- **The account's own pages and API key.** An administrator account's API key
  still carries the `admin` role's model access, and `/user` routes still take
  the session alone.
- **Detection.** Rows are written to `admin_audit_log` (`two_factor.enable`,
  `.verify` including failures, `.recovery_codes`, `.disable`, `.reset`), each
  with the session id and environment. Nothing notifies anyone.
- **Refresh-cookie reuse.** A rotated cookie presented again is refused, as
  before, but does not end the browser session. Doing that safely needs a grace
  window for concurrent tabs and affects every user, so it is its own change.
