# Organizations

This page is for an operator whose gateway serves the members of a university
or a company through [single sign-on](sso.md), and who wants that organization
to see and manage its own members. After reading it you can make an
organization, name its administrators and set its limits, and you know what
its administrators see and can change.

A deployment that has no organization behaves as it did before.

## What an organization is

An organization is a record of its own in the gateway. It owns one or more SSO
connections and the accounts of its members. An SSO connection is one entry of
`SSO_PROVIDERS`, the provider that [Single Sign-On](sso.md) describes.

- **A member** is an account that has an email in the `domains` of an SSO
  connection of the organization, and an organization account of that
  connection linked to it. An account becomes a member at its first sign-in
  through the connection, when its owner connects the organization account
  under **Settings → Single sign-on**, or when you attach the connection to
  the organization.
- **An organization administrator** is a member that you give that role. It
  sees the usage of the members and manages them. The role gives no access to
  **Admin**, and the `admin` role gives no role in an organization.
- **An account is in one organization at most, and so is an SSO connection.**
  A person who also wants a personal account registers a second account with
  another email.

A membership ends only when the account is deleted permanently. The members
stay when you detach an SSO connection, and when the email of an account
changes later.

## Set an organization up

The Admin console has no pages for these steps yet. Call the Admin API. Each
call takes `Authorization: Bearer <token>`. The token is `ADMIN_TOKEN`, or the
access token of an administrator's console session, which `POST /auth/login`
returns as `access_token`.

1. **Connect the identity provider** as [Single Sign-On](sso.md) describes,
   and note the `id` of its entry in `SSO_PROVIDERS`.
2. **Make the organization** from that SSO connection:

   ```bash
   curl -s https://your-gateway.example/admin/organizations \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name": "Example University", "provider_ids": ["example-univ"], "monthly_limit_usd": "1000"}'
   ```

   The answer holds the `id` of the organization, and `members_added`: the
   number of accounts that had signed in through the connection and are now
   members. Leave `monthly_limit_usd` out for an organization without a limit.
3. **Give the first administrator the role.** The person signs in through the
   SSO connection once, which makes the account a member. Find the account in
   **Admin → Users** and copy its user id. Then:

   ```bash
   curl -s -X PUT \
     "https://your-gateway.example/admin/organizations/$ORGANIZATION_ID/members/$USER_ID/role" \
     -H "Authorization: Bearer $ACCESS_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"role": "admin", "password": "<your password>"}'
   ```

   This call asks you to confirm who you are, as raising the role of an
   account does in **Admin → Users**. Send it with the access token of your
   own console session: `ADMIN_TOKEN` cannot give the role. An account with a
   password sends it in `password`. An account with an authenticator app gives
   a code in that session first, and needs no `password`. `{"role": "member"}`
   takes the role back and asks for no confirmation.

The other calls:

| Call | What it does |
|---|---|
| `GET /admin/organizations` | Lists the organizations: name, number of members, SSO connections, administrators, limits and the spend of this month |
| `GET /admin/organizations/{id}` | Shows one organization |
| `PATCH /admin/organizations/{id}` | Changes `name` or `monthly_limit_usd`. `null` clears the limit |
| `PUT /admin/organizations/{id}/connections/{provider_id}` | Attaches an SSO connection. Each account that matches the member rule and is in no organization becomes a member |
| `DELETE /admin/organizations/{id}/connections/{provider_id}` | Detaches an SSO connection. The members stay. New accounts of the connection do not become members |

An organization stores the `id` of each of its SSO connections. If you change
the `id` of an entry in `SSO_PROVIDERS`, the organization loses that
connection and shows it as not available. The members stay. Attach the new
`id`.

## What an organization administrator sees

An organization administrator has an entry in the navigation of the console,
under **Organization**, with the name of the organization. The entry opens
the organization console. No other account sees the entry.

The pages of the organization console read the routes below
`/user/organization`. A script can call the same routes with the access token
of the administrator's console session. The organization is always the one of
the caller's membership: no call names it. Every other caller gets a 403.

| Page | Route | Shows |
|---|---|---|
| **Settings** | `GET /user/organization` | The organization: name, SSO connections, administrators, limits and the spend of this month |
| **Overview** | `GET /user/organization/overview` | Totals of the period: spend, requests, tokens and the number of members with requests. A series of each day, or of each hour for one day. The spend of each model |
| **Members** | `GET /user/organization/members` | One row for each member: name, email, role, status, last request, requests, tokens, spend, and the spend of this month with the monthly limit. Takes `search`, `status`, `sort_by`, `order`, `limit` and `offset` |
| **Members**, in the panel of a member | `GET /user/organization/members/{user_id}` | One member: the same figures, the spend of each of the last 30 days, the spend of each model, and the last requests |
| **Usage → Requests** | `GET /user/organization/usage/requests` | One row for each request, newest first: time, member, project, name of the API key, model, status code, tokens and cost. Takes `search`, `model`, `status`, `limit`, and `before` for the next page |
| **Usage → By model** | `GET /user/organization/usage/models` | Requests, errors, tokens and spend of each model |
| **Usage → By member** | `GET /user/organization/usage/members` | Requests, tokens and spend of each member |
| **Audit log** | `GET /user/organization/audit-log` | The management actions of this organization. Takes `category`, `search`, `limit` and `offset` |

The routes that show figures take `period`: `1d`, `7d` or `30d`, the last 1, 7
or 30 days in UTC, with today. The audit log takes it too, and shows every row
without it. "This month" is the calendar month in UTC, the month that an
account's monthly quota uses. The figures include what an account used before
it became a member.

Money fields follow [Accounting Currency](accounting.md): the `*_usd` fields,
and the native amounts with `currency` on a ledger that is not in USD.

No route shows these:

- the content of a request: the prompt, the response, the tools or the error
  text;
- the value of an API key. A request row shows the name of the key only;
- an account that is not a member, or a different organization;
- the configuration of the deployment, or the client secret of an SSO
  connection.

## What an organization administrator can change

On **Members**, a click on a row opens a panel with the details of the member
and the actions on that member: **Set monthly limit**, **Remove from
organization** and **Restore member**. **Settings** has the default monthly
limit for a member. The actions call these routes:

| Route | What it does |
|---|---|
| `PUT /user/organization/members/{user_id}/monthly-limit` | Sets the monthly limit of a member. `{"monthly_limit_usd": null}` clears it |
| `PUT /user/organization/default-member-limit` | Sets the default monthly limit for a member. `null` clears it |
| `POST /user/organization/members/{user_id}/remove` | Removes a member |
| `POST /user/organization/members/{user_id}/restore` | Restores a member that the organization removed |

None of these applies to an organization administrator, to an account with
the `admin` role, or to the caller. The answer is a 409. The console shows no
action in the panel of an organization administrator.

### Remove and restore a member

To remove a member is to suspend its account, as **Admin → Users** does:

- the account can no longer sign in, through single sign-on either;
- its sessions end and its API keys are revoked;
- its usage history stays.

To restore a member makes the account active again. The revoked API keys stay
revoked, and the member makes new ones.

An organization administrator restores only a member that the organization
removed. An account that you suspended in **Admin → Users** is yours to make
active again. When you make a removed member active again there, the
organization sees the member as active too.

The gateway is not told when the identity provider disables an account. Until
an administrator removes the member, its open sessions and its API keys keep
working.

## Limits

A request of a member must fit under the limits of its API key, its project
and its account, as every request must, and then under the monthly limit of
its organization.

| Limit | Who sets it | Applies to |
|---|---|---|
| The monthly limit of the organization | You, with `monthly_limit_usd` | What all members are charged in a calendar month in UTC |
| The default monthly limit for a member | An organization administrator | Each member that has no monthly limit of its own |
| The monthly limit of a member | An organization administrator | That member. It is the monthly quota of the account, the one **Admin → Users** shows |

An organization administrator cannot set a limit of a member, or the default,
above the limit of the organization. The daily quota of an account does not
change: it comes from the role of the account or from **Admin → Users**.

A request that the limit of the organization refuses gets the 429 of the other
limits, with `scope` set to `organization`:

```json
{
  "error": "Organization monthly limit exceeded",
  "code": "budget_exceeded",
  "scope": "organization",
  "window": "month",
  "organization": "Example University",
  "quota_usd": 1000.0,
  "spent_usd": 1000.02,
  "remaining_usd": 0,
  "reset_at": "2026-12-01T00:00:00+00:00",
  "contact_email": "support@your-gateway.example",
  "message": "Organization 'Example University' reached its monthly limit of $1,000.00. Contact an administrator of your organization.",
  "retry_after": 1123200
}
```

A member whose own limit is reached gets the refusal of an account, with
`scope` set to `account`.

Things to know:

- **The spend of an organization starts at membership.** What an account was
  charged before it became a member does not count against the limit of the
  organization. The usage pages show it all the same.
- **A change of a limit applies within 30 seconds.**
- **Requests that run at the same time** can take an organization a little
  past its limit, as they can for the quota of an account.
- **A batch** of a member waits while the organization is at its limit, as it
  does for the monthly quota of the account. It continues when the limit is
  raised or the next month starts.
- **A balance poll**, such as `GET /v1/usage`, reports the limits of the key,
  the project and the account. It does not report the limit of the
  organization.
- **On a ledger that is not in USD**, send a limit in the ledger currency as
  `monthly_limit` with `currency`. The offline currency migration converts the
  limits of the organizations that exist.

## The audit log

Each action on this page writes a row to the audit log, with the organization
it belongs to. You read the rows of one organization with
`GET /admin/audit-log?organization_id=<id>`. An organization administrator
reads the rows of its own organization. A row of yours names its actor
`platform_administrator`, without an account id or an email.

| Action | Written when |
|---|---|
| `organization.create`, `organization.update` | You make an organization or change its name |
| `organization.connection.attach`, `organization.connection.detach` | You attach or detach an SSO connection. An attach writes one row with the number of new members |
| `organization.admin.add`, `organization.admin.remove` | You give the administrator role or take it back |
| `organization.limit.set` | You change the monthly limit of the organization |
| `organization.default_member_limit.set`, `organization.member_limit.set` | An organization administrator changes the default limit or the limit of a member |
| `organization.member.join` | An account becomes a member at a sign-in or a link |
| `organization.member.remove`, `organization.member.restore` | An organization administrator removes or restores a member |

## Stop using an organization

Take the administrator role back from each of its administrators and clear its
monthly limit. The members keep their accounts and sign in as before.
