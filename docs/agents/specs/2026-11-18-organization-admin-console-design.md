# Organization admin console: usage visibility and member management

- Date: 2026-11-18
- Status: proposed, awaiting review. Implemented by the two pull requests in §17
- Issue: #1829 (RFC). The platform side, #1830, is a separate record
- Scope: an organization record with members and administrators; a console area where an organization
  administrator sees the usage of the members and manages them; a monthly limit of the organization that the
  gateway enforces; a member signs in through single sign-on only. Unchanged: the `/v1` request and response
  formats. The Admin Console changes in one place: **Admin → Users** shows the organization of an account
- Revised: 2026-11-22, after the review of the two pull requests. The tables have the names of §12; a member
  has no password (§4.4); the organization console gained the figures and the two member actions that
  **Admin → Users** has (§6, §7.5, §7.6); the two consoles share components (§13)
- Based on: `dev` @ `e911f8e5`
- Related records: [enterprise single sign-on](2026-11-13-enterprise-sso-oidc-design.md) (SSO connections,
  identities, linking), [projects and API keys](2026-11-01-projects-and-api-keys-design.md) (§18 limits,
  §19 the organization that was removed), [audit attribution](2026-10-25-audit-attribution-design.md),
  [versioned schema migrations](2026-11-02-versioned-schema-migrations-design.zh.md),
  [shared spend](../plans/2026-11-14-shared-spend-admission-and-settlement.md),
  [accounting currency](../../developer/accounting.md),
  [console language switching](2026-10-17-console-language-switching-design.zh.md)

## 1. Background and goals

With single sign-on, the members of a university or a company sign in with the accounts of their
organization. The gateway does not know the organization as a thing of its own. One `SSO_PROVIDERS` entry
stands for it, and no record holds its administrators or its limit.

So only the platform administrator (the `admin` role) can see these members and stop the account of a member
who left, in **Admin → Users**. That page shows every account of the deployment. The administrator of one
organization cannot be given that page.

Goals:

1. An organization is a record: a name, limits, its SSO connections and its administrators.
2. An organization administrator sees the members of that organization and their usage. The administrator
   does not see request content.
3. An organization administrator sets the monthly limit of a member, removes a member and restores a member.
4. An organization has a monthly limit. The gateway refuses a request when the limit is reached.
5. Each management action is a row in an audit log that the organization administrator can read.
6. A deployment that has no organization behaves exactly as before.

## 2. Not in this work

| Left out | Note |
|---|---|
| The Admin pages for organizations: the list, suspend, delete | #1830. This record gives the Admin routes that set an organization up (§9), and the organization column in **Admin → Users** |
| Automatic removal when the identity provider disables an account (SCIM) | Later. Until then an administrator removes the member (§7.3) |
| Invitations, and approval in the organization console | Each SSO connection decides with `auto_create` and `require_approval`, as today |
| One account in two organizations, and an account that joins or leaves an organization | The organization owns the account (§3) |
| Projects or API keys that members share | Later. A project belongs to one account |
| Roles other than member and administrator, and roles from the groups of the identity provider | Later |
| An organization that has no SSO connection | Membership comes only from SSO |
| A model list for the whole organization, and limits on concurrency or request rate | Later. An organization turns a model off for one member (§7.5) |
| CSV export, sign-in events in the audit log, and the source address of an audit row | The prototype of #1829 shows the first two. They can follow |
| The behavior figures of **Admin → Users** (turns, automation score, anomalies) and its saved views | They read request content, or they are tools of the platform administrator |
| A second factor for organization administrators | The second factor of #1814 is for the platform administrator |

## 3. Model

| Term | Meaning |
|---|---|
| Platform administrator | An account with the `admin` role. It manages the deployment in `/admin` |
| Organization | A record with a name, a monthly limit and a default monthly limit for a member |
| SSO connection | One `SSO_PROVIDERS` entry. It belongs to one organization at most |
| Member | An account that has a membership record in one organization |
| Organization administrator | A member whose membership has the role `admin` |

Rules:

1. **One organization for each account.** A personal account and an organization account are two accounts.
2. **The member rule.** An account becomes a member when it has an email in the `domains` of an SSO
   connection of the organization and an identity of that connection (§4).
3. **The two administrator roles are independent.** The role of organization administrator gives no access
   to `/admin`. The `admin` role gives no role in an organization.
4. **The organization of a request** is the organization of the account that the request is charged to.
5. **An organization administrator works in one organization.** Each route reads the organization from the
   membership of the caller. A request never names the organization.

## 4. Membership

### 4.1 How an account becomes a member

The gateway writes the membership record in the same transaction as the event that makes the member rule
true:

| Event | Where |
|---|---|
| A first sign-in through the SSO connection creates the account | `create_sso_user` |
| An account with an email in the `domains` links an identity of the connection: automatic linking, guided linking, or **Settings → Single sign-on** | `link_user_identity` |
| The platform administrator attaches an SSO connection to an organization | §9. Each account that has an identity of the connection and an email in its `domains` becomes a member |

A sign-in and an attach of the same connection can run at the same time. One database lock puts the two in
order, so the account becomes a member in each order.

A new membership has the role `member`. If the account is a member of a different organization, the
gateway refuses the link.

An account that waits for approval (`require_approval`) is a member with the status "Pending approval". The
platform administrator approves it in **Admin → Users**, as today.

### 4.2 What ends a membership

A membership ends when the account is deleted permanently. Nothing else ends it:

- A removed member keeps the membership. The account is suspended (§7.3).
- A later change of the email of the account does not end the membership.
- When the platform administrator detaches an SSO connection, the members stay. New accounts of that
  connection do not become members.

### 4.3 Two changes to the behavior of #1719

- **Link.** Today **Settings → Single sign-on** lets an account connect an identity of a connection when the
  email of the account is outside its `domains`. The gateway now refuses a new link of this kind. Links of
  this kind that exist stay, and these accounts are not members. The rule applies to each deployment, also
  to one that has no organization. Thus no new link of this kind is made before a connection is attached.
- **Disconnect.** A member cannot disconnect the identity of an SSO connection of its organization in
  **Settings → Single sign-on**. The member rule needs this identity.

### 4.4 A member signs in through single sign-on only

The organization owns the account of a member. So the identity provider of the organization decides who
can sign in to it: a person that the provider disabled must not get in with a password that the account
had from before.

The rule applies to each member that is not a platform administrator (the `admin` role, or an address in
`ADMIN_EMAILS`). A platform administrator keeps the password: that account must get in when the identity
provider is not available.

- **At the join.** The event that makes an account a member (§4.1) removes its password and its password
  reset links, in the same transaction. A first sign-in through SSO makes an account that has none.
- **Password sign-in.** `POST /auth/login` refuses a member with the answer for a wrong password. Thus a
  password that stayed on the account (for example, of an account that lost the `admin` role) does not sign
  in.
- **Phone sign-in.** `POST /auth/phone/login` refuses a member. A member can have a phone number, and the
  code then confirms an action, but it does not sign in.
- **No new password.** `POST /user/change-password` and `POST /auth/reset-password` refuse a member with a
  403. `POST /auth/forgot-password` gives its usual answer and sends no link.
- `GET /user/me` has `sso_only: true` for such an account. The console then shows no password control.

Sessions that exist stay. An API key is not a sign-in: the keys of a member work until the member is removed
(§7.3) or a key is revoked (§7.6).

A member can have an identity of an SSO connection that is not attached to the organization, when that
connection answers for the same email domain. That identity signs in. The platform administrator attaches
each connection that answers for the domains of an organization.

## 5. Who may call what

- **The organization console routes** (§11.1) need a console session and a membership with the role `admin`.
  A new dependency, `require_organization_admin`, does this check and returns the organization. Every other
  caller gets a 403.
- **A member in a path** (`/members/{user_id}`) must be a member of the organization of the caller.
  Otherwise the answer is a 404, as for an account that does not exist.
- **The Admin routes** (§11.2) use `verify_admin_access`, as each Admin route does.
- **The member actions** (§7) do not apply to an organization administrator, to an account with the `admin`
  role, or to the caller. The answer is a 409.
- `GET /user/me` gets a field `organization`: `{id, name, role}` for a member, `null` for each other
  account. The console shows the entry of the organization console when the role is `admin`. It also gets
  `sso_only` (§4.4).

## 6. What an organization administrator sees

### 6.1 Periods and sources

Overview, Usage and Audit log have a period: `1d`, `7d` or `30d`. A period is the last 1, 7 or 30 days in
UTC, with today. "This month" is the calendar month in UTC, the same month as the account quota uses.

Members has no period. Each row has the spend columns of **Admin → Users**: today, the last 7 days, this
month and all time (§6.3).

Every figure is a sum over the accounts that have a membership in the organization, from the time each
account became a member. The pages do not show what an account used before. When the platform administrator
attaches a connection, accounts become members that did nothing for it, and their earlier requests name
models, projects and API keys.

- A request row has its instant, thus the request log is cut at the instant of the membership.
- A day counter is one row for a UTC day. Thus the day on which an account became a member counts as a
  whole day.
- The spend of this month of a member is not cut. It is the figure that the monthly limit is compared with,
  and the gateway counts the whole calendar month.

| Figure | Source |
|---|---|
| Spend for each day | `user_daily_cost`, the counters that the quota gates read |
| Requests and tokens, the figures for each model and for each hour, the request volume, and the request rows | `api_logs`, through the index on `(user_id, timestamp)`. A period is 30 days at most |
| Spend of a member today, in the last 7 days and in all time | `user_daily_cost`, from the day of the membership |
| Spend of a project and of an API key in this month | `spend_counters`, the rows that the quota gates read |
| Last request of a member | `user_daily_cost.last_request_at` |
| Monthly limit of a member | The limit on the membership, the default of the organization, and the monthly quota of the account (§8.1) |
| Spend of the organization in this month | The counter of the organization (§8.2) |

The requests do not come from `user_daily_cost`, which counts only the requests that had a cost. They do not
come from `user_usage_daily`, which holds completed days only.

Money fields follow the [accounting contract](../../developer/accounting.md): the `*_usd` fields, and the
native amounts with `currency` on a ledger that is not in USD.

### 6.2 Overview

- **Totals for the period:** spend, requests, tokens, the number of members with requests, and the number
  of members. For `1d` and `7d`, also the spend and the requests of the period before.
- **This month:** the spend of the organization and its limit.
- **Two series:** members with requests, and spend. One point for each day. For `1d`, one point for each
  hour of the UTC day that has begun. `granularity=hour` gives one point for each hour also for `7d`. It is
  not available for `30d`.
- **A table for each model** in the period: requests, errors, input tokens, output tokens, cached tokens,
  spend and the share of spend.
- **Top members:** the 5 members with the highest spend in the period, with their requests and tokens.

### 6.3 Members

**Two cards** above the list (`GET /user/organization/members/summary`):

- **Near or over the limit:** the number of members whose spend of this month is 80% or more of the monthly
  limit that applies to them, and the 5 with the highest share.
- **Top spenders today:** the number of members with spend today, and the 5 with the highest spend.

A card opens the list with the filter and the order that it stands for.

**One row for each member:** name, email, role, status, last request, and four spend columns: today, the
last 7 days (the total, and one value for each day), this month with the monthly limit, and all time. The
row also has the requests and the tokens of all time.

The list takes:

- a search text (name or email). A search text is matched as it is, here and on the other pages: `%` and
  `_` in it have no special meaning;
- a status filter;
- `limit_state`: `near` (80% or more of the limit) or `over` (100% or more);
- `min_cost_today` and `min_cost_month`, in the ledger currency. `currency` names the unit that the console
  read them in. When it is not the ledger currency, the answer is a 409, as in **Admin → Users**;
- a sort column and a page.

The status is the status of the account. A member that the organization removed shows "Removed". The
status filter has one value for each word that the list shows. Thus the value `suspended` does not list a
member that the organization removed.

The sort column can be each spend column, the last request, the name, the join date, or the share of the
monthly limit that the spend of this month uses (`sort_by=limit_use`). A member without a limit is last.

A row has `protected: true` for an administrator of the organization or of the platform. The organization
cannot set the limit of such a member, change its models, revoke its keys or remove it: the routes of §7
answer with a 409. The console shows no action for it.

A row opens the details of the member (`?period=`, default `30d`):

- the join date and the last sign-in;
- whether the account has an active API key, a second factor, and a verified email;
- the four spend columns, and the monthly limit;
- the spend for each day of the last 30 days;
- the spend for each model in the period;
- the projects of the member. For each project: its name and its spend of this month. For each API key of
  the project: its name, its status, its last use and its spend of this month. Never the key value or its
  prefix;
- the models that are off for the member: those that the organization turned off (§7.5), and those that
  the platform administrator turned off, which the organization can read and cannot change;
- the last 6 requests;
- the actions of §7.

### 6.4 Usage

- **Request volume.** Four cards: the last hour, day, week and month. Each has the requests, how many were
  OK and how many failed, and the average latency. The windows end now; they are not the period of the
  page.
- **Requests.** One row for each request, newest first: time, member, project, API key name, model, status
  code, input tokens, output tokens, cost. Filters: member (search text), model, and status (all, OK,
  errors). The page size is 50.
- **By model.** Requests, errors, input tokens, output tokens, spend and the share of spend.
- **By member.** Requests, tokens, spend and the share of spend.

### 6.5 Audit log

The management actions of this organization (§10): time, actor, event, target and details. Filters: a
search text, a category, a member and the period. The member filter gives the rows that have the member as
target or as actor. An entry opens to show each field of the details: for a change, the old value and the
new value. No route returns the source address of a row.

### 6.6 Settings

- **Organization:** name, id, creation date, number of members. Read-only.
- **Single sign-on:** for each SSO connection its id, display name, `domains`, and whether a first sign-in
  creates an account and needs approval. Read-only.
- **Organization administrators:** name, email, last sign-in. Read-only.
- **Limits:** the monthly limit of the organization (read-only) and the default monthly limit for a member
  (§7.2).

### 6.7 What no route returns

- Request content: `prompt`, `response`, `request_payload`, `tools`, `error`, `metadata`.
- API key values and key prefixes. A request row and the details of a member show the name of a key only.
- The source address of a request or of an audit row.
- Data of an account that is not a member, and data of a different organization.
- The configuration of the deployment, and the secret of an SSO connection.

## 7. What an organization administrator can change

### 7.1 The monthly limit of a member

`PUT /user/organization/members/{user_id}/monthly-limit` sets the limit of the organization for one
member. It is a pair of columns on the membership (`org_memberships.monthly_limit_usd` and
`monthly_limit_amount`). An empty value clears it, and the default of the organization then applies.

- The value cannot be negative, and it cannot be greater than the limit of the organization.
- The monthly quota of the account (`account_quotas`) belongs to the platform administrator, who sets it in
  **Admin → Users**. This route does not read it and does not write it. The gateway applies the lower of the
  two values (§8.1). Thus an organization administrator can lower what a member may spend, and cannot raise
  it above the quota of the platform administrator.
- The write is one transaction with the audit row. The account does not need an API key.
- The daily quota of the account does not change.
- The row of a member has the limit that applies and its source (`member`, `default`, `platform` or
  `none`). It also has the limit that the organization set for the member, and the quota of the platform
  administrator, which the organization can read.
- When the platform administrator lowers the limit of the organization, the limits of the members and the
  default stay, also above the new limit. A request must be under each limit, thus the lower limit applies.

### 7.2 The default monthly limit for a member

`PUT /user/organization/default-member-limit` sets the default. It applies at once to each member that the
organization set no limit for. It does not apply to an organization administrator or to an account with the
`admin` role. The organization sets no limit of those accounts (§7.1), thus its default must not stop them
either: one organization administrator could otherwise stop a second one, or the platform administrator,
with a default of zero. The gateway does not copy the default to the members (§8.1). The same two rules on
the value apply.

### 7.3 Remove a member

`POST /user/organization/members/{user_id}/remove` suspends the account. It is the operation that
**Admin → Users** has today (`suspend_user`), in one transaction with the audit row:

- the status of the account becomes `suspended`;
- the gateway revokes the sessions and the API keys;
- the membership gets the time and the actor of the removal;
- the account gets a suspension message that names the organization.

The gateway refuses a sign-in of a suspended account, through SSO too. The usage history stays.

### 7.4 Restore a member

`POST /user/organization/members/{user_id}/restore` makes the account active again and clears the mark of
the removal. The revoked API keys stay revoked. The member makes new keys.

An organization administrator can restore only a member that the organization removed. If the platform
administrator suspended the account in **Admin → Users**, the answer is a 409. When the platform
administrator makes a removed member active again in **Admin → Users**, the gateway clears the mark too.

The gateway also clears the mark when the platform administrator writes a suspension message for a removed
member. An account cannot be suspended two times, and this is how the platform administrator takes the
suspension. The organization can then no longer restore the member.

### 7.5 Turn a model off for a member

`PUT /user/organization/members/{user_id}/models` takes `{"disabled_models": [...]}` and replaces the list
of the organization for that member. An empty list turns nothing off.

- The list is a column on the membership (`org_memberships.disabled_models`). The list of the platform
  administrator is in the preferences of the account, and **Admin → Users** sets it. This route does not
  read it and does not write it.
- The gateway refuses a model that is in either list (§8.4). Thus an organization can turn a model off, and
  cannot turn on a model that the platform administrator turned off.
- An id that is not a model of the deployment is refused with a 422.
- The write is one transaction with the audit row, which has the old list and the new list.

### 7.6 Revoke an API key of a member

`POST /user/organization/members/{user_id}/keys/{key_id}/revoke` revokes one API key. It is the operation
that the owner of the key has in the console. The key must belong to the member: for each other key the
answer is a 404. A revoked key stays revoked, and the member can make a new one.

The audit row has the name of the key and of its project.

## 8. Limits at admission

Today each gate checks three levels in order: the API key, the project, the account
(`quota.first_refusal`). This work changes the last level for a member and adds a fourth level.

### 8.1 The monthly limit of a member

The monthly quota of an account is `account_quotas.quota_monthly_cost_usd`; NULL is no limit. The platform
administrator sets it. For a member, the gateway enforces the lower of two values:

- the quota of the account;
- the limit of the organization for the member: the limit on the membership (§7.1), else the default of the
  organization (§7.2). An organization administrator and an account with the `admin` role have none.

If neither exists, the member has no monthly limit. The two values are compared in the native amounts when
both have one, and in USD if not. A native amount is thus not compared through its rounded USD value. A
refusal by either value has `scope: "account"`.

The two limits have different owners, thus they are in different places. With one field for both, an
organization administrator could clear or raise a quota that the platform administrator set.

The lookup of an API key reads the membership and the organization through their primary keys, in the same
round trip (`member_quota_join`). Thus a change of a limit of the organization applies to its members
without a write to each account.

The quota of the account is what the request path reads today: the record of the account, or the copy on a
key for an account that has no record. The member list reads the same two places.

A member without an API key spends through the playground, or through a service key that acts on its
behalf. No key lookup answers for such an account, so one read by account
(`get_organization_spend_scope`) gives its organization and its monthly quota: the lower of the two values.

### 8.2 The monthly limit of the organization

- **The counter.** The spend of an organization in a month is one row of `spend_counters`: scope
  `o:<organization id>`, period `m:YYYY-MM`. Projects have such rows today.
- **Write.** The statement that adds a cost to the counters of the account, the key and the project
  (`COST_INCREMENTS_SQL` and its variants) also adds it to the counter of the organization. It finds the
  organization through the membership of the account at that time.
- **Read.** The lookup of an API key reads the limit and the counter with the membership, in the same round
  trip.
- **Check.** `quota.first_refusal` gets a fourth level after the account: the organization, for the month
  only. `SpendCheck` carries the limit and the spend of the organization.
- **Requests in flight.** The per-process ledger counts the reservations and the costs that are not yet
  written for the organization. The reservations are a total for each organization, as for a project. The
  costs are kept by account, so the ledger adds them for the members that a gate of the process has seen.
- **Every gate.** The API key gate, a request on behalf of an account, the grant gate, the stored-key gate
  of batches and the session gate of the playground build the same `SpendCheck`, so all of them apply the
  limit. A batch pauses as it does for the monthly quota of the account.
- **The cached lookup.** The identity lookup that the gateway caches carries the organization and its limit,
  not its spend. For an identity from that lookup, the gate reads the counter, as it reads the spend of a
  project.

Three properties to know:

- The counter starts when the account becomes a member. Spend from before that time does not count against
  the limit of the organization.
- A cached key lookup lives 30 seconds. After a change of a limit, of the role of a member or of the name of
  the organization, the gateway drops the cached lookups of the accounts that the change applies to.
- Requests that run at the same time can exceed the limit by approximately the cost of one request. This is
  the bound that the other levels have.
- The shared spend accounting, which is off and which no setting turns on today, checks the limit of the
  organization against the counter that the gate read. It does not add the requests in flight of the other
  processes for the organization. To add them is part of the work that turns that mode on.
- A balance poll (`GET /v1/usage` and the routes beside it) reports the limits of the key, the project and
  the account. It does not report the limit of the organization, because that would show the spend of the
  organization to each member.
- The limits of an organization are in the ledger currency (§12). The offline currency migration converts
  them.

### 8.3 The refusal

The answer is a 429 with `scope: "organization"` and `window: "month"`. It names the organization and says
when the month starts again. It has no amount in the body, the message or the headers: each member can get
this answer, and the limit and the spend of an organization are for its administrators (as for a balance
poll, §8.2).

```json
{
  "error": "Organization monthly limit exceeded",
  "code": "budget_exceeded",
  "scope": "organization",
  "window": "month",
  "organization": "Example University",
  "reset_at": "2026-12-01T00:00:00+00:00",
  "contact_email": "support@example.edu",
  "message": "Organization 'Example University' reached its monthly limit. Contact an administrator of your organization.",
  "retry_after": 1123200
}
```

A member whose own limit is reached gets the refusal of today, with `scope: "account"`.

### 8.4 The models of a member

Each gate reads the models that are off for the account from the preferences of the account
(`disabled_models`). For a member, the gate adds the list of the membership (§7.5). The lookup of an API
key reads it with the membership, in the same round trip, and a gate that reads the account reads the
membership with it. The refusal is the one of today for a model that is off, and `GET /v1/models` and
`GET /user/models` leave the model out.

A cached key lookup carries the list. A change drops the cached lookups of the member (§8.2).

## 9. The platform administrator

The platform administrator sets an organization up through Admin routes (§11.2). The pages for these routes
are in #1830. Until then the operator guide gives the calls.

1. **Make the organization:** a name, one SSO connection or more, and an optional monthly limit.
2. **Attach or detach an SSO connection.** A connection that belongs to a different organization is
   refused. On attach, the accounts that match the member rule become members (§4.1).
3. **Give or remove the role of organization administrator.** The account must be a member. So the first
   administrator signs in through the SSO connection one time before the role is given. To give the role,
   the platform administrator confirms with a code or a password (`confirm_administrator`), as for a higher
   role of an account in **Admin → Users**: the role stays after the session ends. `ADMIN_TOKEN` cannot give
   it. To remove the role needs no confirmation.
4. **Change the name and the monthly limit.**

**Admin → Users** shows the organization of each account: a column, a filter (one organization, or the
accounts that have none), and the organization with the role in the details of an account.

An organization stores the `id` of each of its SSO connections. If the platform administrator changes the
`id` of an entry in `SSO_PROVIDERS`, the organization loses that connection. The members stay. The platform
administrator attaches the new `id`. The routes show a connection that has no entry as not available.

## 10. Audit

`admin_audit_log` gets a column `organization_id`. A row with a value belongs to that organization.
`TargetType` gets the value `organization`.

| Category | Action | Actor | Target |
|---|---|---|---|
| Membership | `organization.member.join` | the member | the member |
| Membership | `organization.member.remove`, `organization.member.restore` | the organization administrator or the platform administrator | the member |
| Membership | `organization.admin.add`, `organization.admin.remove` | the platform administrator | the member |
| Limits | `organization.member_limit.set` | the organization administrator | the member |
| Limits | `organization.default_member_limit.set` | the organization administrator | the organization |
| Limits | `organization.limit.set` | the platform administrator | the organization |
| Access | `organization.member_models.set` | the organization administrator | the member |
| Access | `organization.member_key.revoke` | the organization administrator | the member |
| Organization | `organization.create`, `organization.update`, `organization.connection.attach`, `organization.connection.detach` | the platform administrator | the organization |

- The details hold the old value and the new value.
- An attach writes one row, with the number of new members in the details. It writes no join row for each
  member.
- An organization administrator reads only the rows of that organization, through
  `GET /user/organization/audit-log`. The response names a platform administrator `platform_administrator`,
  with no account id and no email. The console shows it as "Platform administrator".
- The platform administrator reads all rows. `GET /admin/audit-log` gets an `organization_id` filter.
- The permanent delete of an account removes the audit rows that have the account as target. This is the
  rule of today.

## 11. HTTP surface

### 11.1 Organization console

The routes are below `/user/organization`, in a router of their own, as the project routes are below
`/user/projects`. The console forwards `/user` today, so `deploy/public-paths.json` and the route handlers of
the console do not change.

| Route | Returns or does |
|---|---|
| `GET /user/organization` | The organization, its SSO connections, its administrators, its limits and the spend of this month |
| `GET /user/organization/overview?period=&granularity=` | §6.2 |
| `GET /user/organization/members?search=&status=&limit_state=&min_cost_today=&min_cost_month=&currency=&sort_by=&order=&limit=&offset=` | §6.3, with the total number |
| `GET /user/organization/members/summary` | The two cards of §6.3 |
| `GET /user/organization/members/{user_id}?period=` | The details of one member |
| `GET /user/organization/usage/volume` | The four cards of §6.4 |
| `GET /user/organization/usage/requests?period=&search=&model=&status=&limit=&before=` | Request rows. `before` takes the cursor that the page before returned |
| `GET /user/organization/usage/models?period=` | Totals for each model |
| `GET /user/organization/usage/members?period=&search=` | Totals for each member |
| `GET /user/organization/audit-log?period=&category=&member=&search=&limit=&offset=` | §6.5 |
| `PUT /user/organization/members/{user_id}/monthly-limit` | §7.1 |
| `PUT /user/organization/default-member-limit` | §7.2 |
| `POST /user/organization/members/{user_id}/remove` | §7.3 |
| `POST /user/organization/members/{user_id}/restore` | §7.4 |
| `PUT /user/organization/members/{user_id}/models` | §7.5 |
| `POST /user/organization/members/{user_id}/keys/{key_id}/revoke` | §7.6 |

### 11.2 Admin

| Route | Returns or does |
|---|---|
| `GET /admin/organizations` | The organizations: name, number of members, limits, SSO connections |
| `POST /admin/organizations` | Makes an organization |
| `GET /admin/organizations/{id}` | One organization, in the shape of `GET /user/organization` |
| `PATCH /admin/organizations/{id}` | Name and monthly limit |
| `PUT /admin/organizations/{id}/connections/{provider_id}` | Attaches an SSO connection |
| `DELETE /admin/organizations/{id}/connections/{provider_id}` | Detaches an SSO connection |
| `PUT /admin/organizations/{id}/members/{user_id}/role` | `member` or `admin`. `admin` takes the `password` of the platform administrator (§9) |

The functions behind §11.1 take the organization as an argument. So #1830 can show the same pages to the
platform administrator with thin routes below `/admin/organizations/{id}`.

### 11.3 Changed routes

- `GET /user/me`: the fields `organization` (§5) and `sso_only` (§4.4).
- `POST /auth/login`, `POST /auth/phone/login`, `POST /auth/forgot-password`, `POST /auth/reset-password`
  and `POST /user/change-password`: the refusals of §4.4.
- `POST /auth/sso/identities/link` and `POST /auth/sso/identities/{id}/unlink`: the two refusals of §4.3.
- `GET /admin/users`: the field `organization` (`{id, name, role}` or `null`) on each row, and the filter
  `organization_id` (an id, or `none`). `GET /admin/users/{id}/detail`: the same field.
- `PATCH /admin/users/{id}`: a change of the status to `active` clears the mark of a removal (§7.4).
- `GET /admin/audit-log`: the `organization_id` filter, and the field `organization_id` on each row.
- The 429 of each inference route: the new `scope` value (§8.3).
- `POST /v1/batches` from the console: a refusal by the limit of the organization has the code
  `monthly_quota_exceeded`, as a refusal by the monthly quota of the account.

## 12. Data model

One expand revision, `0023_orgs`. The previous release does not read the new tables or the
new column.

```sql
CREATE TABLE orgs (
    id                                  TEXT PRIMARY KEY,
    name                                TEXT NOT NULL,
    monthly_limit_usd                   DECIMAL(14, 4),     -- NULL: no limit
    monthly_limit_amount                NUMERIC(24, 12),
    default_member_monthly_limit_usd    DECIMAL(10, 4),     -- NULL: no default
    default_member_monthly_limit_amount NUMERIC(24, 12),
    accounting_currency                 TEXT,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX idx_orgs_name ON orgs (lower(name));

CREATE TABLE org_sso_connections (
    provider_id     TEXT PRIMARY KEY,                       -- an id in SSO_PROVIDERS
    organization_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE org_memberships (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at      TIMESTAMPTZ,                            -- set by §7.3, cleared by §7.4
    removed_by      TEXT,
    monthly_limit_usd    DECIMAL(10, 4),                    -- §7.1; NULL: the default applies
    monthly_limit_amount NUMERIC(24, 12),
    accounting_currency  TEXT,
    disabled_models      JSONB NOT NULL DEFAULT '[]'::jsonb -- §7.5
);
CREATE INDEX idx_org_sso_connections_organization
    ON org_sso_connections (organization_id);
CREATE INDEX idx_org_memberships_organization ON org_memberships (organization_id);

ALTER TABLE admin_audit_log ADD COLUMN organization_id TEXT;
CREATE INDEX idx_admin_audit_log_organization ON admin_audit_log (organization_id, timestamp DESC)
    WHERE organization_id IS NOT NULL;
```

Notes:

- **The primary keys carry two rules.** `user_id` is the key of a membership: one organization for each
  account. `provider_id` is the key of a connection: one organization for each SSO connection.
- **No status column.** An organization has no status in this work, because no route changes one and no
  check reads one. #1830 adds the column with the routes that suspend an organization.
- **The money columns** are pairs, as in `projects`: a `*_usd` column and a native `*_amount` column that
  an `accounting_sync_amounts` trigger keeps in step. This applies to the limits of an organization and to
  the limit on a membership. `storage/accounting_schema.py` gets the two tables and their triggers. Thus
  the startup check covers them and the offline currency migration converts them. Without the entry, the
  trigger would refuse each later write to such a row on a ledger that moved to a new currency.
- **The names.** The tables `organizations` and `org_members` of revision `0015` hold the hidden personal
  organization that §19 of the projects record removed. The service continues to write them, the three
  triggers of revision `0018` connect them to the account quota and to the projects, and a contract revision
  is planned to drop them. This design does not use them, so it does not depend on that revision (§18).
  Those two names are thus not free, and the new tables are `orgs`, `org_sso_connections` and
  `org_memberships`. The API, the code and the console say "organization".
- `spend_counters` gets no new column. A row of an organization differs only in its `scope_id`. An
  organization has a row for each month, and no row for a day.

## 13. Console

- **Entry.** The sidebar of the personal console gets an **Organization** group with one item, when
  `user.organization.role` is `admin` (`ConsoleNav.tsx`).
- **Area.** `/dashboard/organization` is an area of its own, with the shell of the Admin Console: full
  width, a link back to the dashboard, and a row of tabs. The tabs are Overview, Members, Usage, Audit log
  and Settings. `/dashboard/organization` opens Overview.
- **Guard.** The layout sends a signed-out visitor to the sign-in page and an account without the role to
  `/dashboard`, as the Admin layout does. The gateway decides access. The guard only chooses what to show.
- **Building blocks.** The pages use what the Admin Console has: `adminStyles.ts`, `AdminPrimitives`,
  `ConfirmDialog` for the dialogs, `useAccountToast`, and `recharts` for the charts.
- **Shared components.** The two consoles show the same kinds of things, so the parts are one copy in
  `components/features/console-shared/`: the summary cards, the spend cells with the 7-day trend, the
  filters of the account list, the projects and keys of an account, the request list, the audit list with
  an entry that opens, the request volume cards, and the usage charts. A shared component takes its data
  and its actions as props, and its text through `t()`. The Admin Console is under a provider that keeps it
  in English, so the same component is in English there and translated in the organization console.
- **Member details.** A member row opens a panel at the right side of the window, as the prototype of
  #1829 shows. The list stays behind the panel. On a phone the panel has the width of the screen. A row in
  **Admin → Users** opens in place. The two containers differ; the parts in them are the shared ones.
- **Dialogs.** "Set monthly limit", "Remove from organization" and "Restore member" say what changes, as the
  prototype of #1829 shows. "Models" and "Revoke key" follow the same pattern.
- **Settings of an account.** The personal **Settings** page shows no password control for an account with
  `sso_only`.
- **Admin → Users.** An Organization column, a filter, and the organization with the role in the details.
- **Language.** The Admin Console is in English only. The organization console is for the staff of an
  organization, so it uses `t()` with slots `organization.<page>.<meaning>` and has entries in
  `zh-CN.json`.
- **Theme.** The pages use only the classes that `console-theme.css` repaints.
- **Older console.** A console from before this work ignores the field `organization` and shows no entry.

## 14. Where the code is

- **Gateway, new:** one revision in `migrations/versions/`; `serving/storage/organizations.py` (a storage
  mixin, as `storage/sso.py`); `serving/organizations.py` (the member rule, the figures of §6, the actions
  of §7); `serving/servers/routers/organization.py` (§11.1);
  `serving/servers/routers/admin/organizations.py` (§11.2); schemas for both.
- **Gateway, changed:**
  - `storage/sso.py`, `auth/sso_accounts.py`, `routers/sso_routes.py`: membership at account creation and at
    link, and the two refusals of §4.3;
  - `storage/postgres_operational.py`: the key lookup fragments and the cost increment statements (§8);
  - `quota.py`, `spend_ledger.py`: the fourth level;
  - `servers/auth.py`, `grant_auth.py`, `routers/playground.py`: the organization in each `SpendCheck`;
  - `batch/scheduler.py`, `routers/batch_console_create.py`: the pause and the refusal of a batch;
  - `servers/deps.py`: `require_organization_admin`;
  - `routers/user_routes.py` and `schemas_auth.py`: the fields of `GET /user/me`, and the refusal of a new
    password;
  - `routers/auth_routes.py`, `routers/phone_auth_routes.py`, `auth/reset_password.py`: the refusals of
    §4.4;
  - `model_access.py` and each gate: the models of a member (§8.4);
  - `routers/admin/users.py`: the mark of a removal, the audit filter, and the organization of an account;
  - `audit.py`: the target type; `storage/audit_schema.py`: the column;
  - `storage/base.py`, `storage/cache.py`, `storage/accounting_schema.py`.
- **Console, new:** `app/dashboard/organization/` (layout, tabs, five pages); `lib/api/organization.ts`;
  `lib/hooks/queries/useOrganization.ts`; components in `components/features/organization/`.
- **Console, changed:** `ConsoleNav.tsx`, `AuthProvider.tsx`, `lib/api/user.ts`, `lib/hooks/index.ts`,
  `components/ui/consoleWidth.ts`, `lib/i18n/messages/zh-CN.json`; the pages of **Admin → Users**,
  **Admin → Requests** and **Admin → Audit**, which now use the shared components; the personal
  **Settings** page.
- **Documentation:** a new `docs/developer/organizations.md` for operators (make an organization, attach a
  connection, give the role, set limits, remove a member), with its Chinese translation and an entry in
  `index.rst`; the two changes of §4.3 and a pointer in `sso.md`; four terms in the glossary; the new
  tables in `database.md`.
- **Not changed:** `routing/`, the adapters, `shared_spend/`, the `/v1` request and response formats,
  `deploy/public-paths.json`, the route handlers of the console, `distributions/`.

## 15. Tests

- **Unit:** `quota.first_refusal` with the fourth level; the refusal body of §8.3; the per-process ledger
  with requests in flight and costs not yet written for one organization; each gate with a row of a member,
  with an identity that came without the spend, and with a member that has no key; the pause of a batch.
- **Database** (`make test-db-local`):
  - the revision;
  - membership at account creation, at link and at attach, and for a sign-in and an attach that run at the
    same time; the two refusals of §4.3, and the refusal for a second organization;
  - each route of §11 for each kind of caller: no session, an account that is not a member, a member, an
    administrator of a different organization, an organization administrator, the platform administrator;
  - a test that no response of §11.1 has a content field or a key value;
  - remove and restore, with the sessions, the keys and the audit row in one transaction;
  - the limit from end to end: a charged request adds to the counter of the organization, and the next
    request gets the 429 of §8.3;
  - the limits on a ledger in CNY, and the offline currency migration on a database that has organizations;
  - the audit rows and their scope.
  - the sign-in rule of §4.4: a password and its reset links are gone after each of the three join events;
    each refusal; a platform administrator keeps the password;
  - the models of a member at each gate, and that the list of the platform administrator stays as it was;
  - the revoke of a key, and that a key of a different account is a 404;
  - the figures of §6.3 from the day of the membership, the filters and the two cards.
- **Admin route count:** `tests/servers/test_admin_router_aggregation.py` gets the new number.
- **Console:** the entry for each role; the guard; each page with a stubbed API; the three dialogs; the API
  module (`lib/api/__tests__/organization.test.ts`); `catalog.test.ts` for the new strings.
- **By hand:** a local deployment with Keycloak as the identity provider. Two members and one organization
  administrator sign in. The walk covers each page, the three actions and a request that the limit of the
  organization refuses.

## 16. Release and rollback

- **No organization, no change.** Without a row in `orgs`, no account is a member, the lookup
  of a key finds no organization, and the console shows no entry.
- **The revision only adds** three tables, one column and two indexes. (The first version of this work
  named the tables `sso_organizations`, `sso_organization_connections` and `sso_organization_members`. The
  revision was renamed in place before it ran on a shared database.) After a rollback to the previous
  release, they are not used.
- **The gateway and the console can be upgraded or rolled back in either order.** An older console ignores
  the new field. A newer console with an older gateway gets no field and shows no entry.
- **A rollback of the gateway** stops the counter of the organization, because the previous release does not
  write it. After the upgrade is done again, the counter of that month is too low by the spend of that
  time. The counter of the next month is correct.
- **To stop the use of an organization,** the platform administrator removes the role of its
  administrators and sets no limit.

## 17. Delivery

Two pull requests that change no file in common, so both can be open at the same time:

1. **Gateway:** the revision, storage, the member rule in the SSO paths, the routes of §11, the member
   actions of §7, the limits at admission (§8), the audit column, their tests, the operator guide and this
   record.
2. **Console:** the entry, the area and its five pages, the dialogs, the strings and their translation, and
   their tests. The shared components of §13 are commits of their own, because they change Admin pages.

A later pull request drops the tables of revision `0015` (`organizations`, `org_members`, `projects.org_id`,
`api_keys.org_id` and the triggers of revision `0018`). It takes two releases: one that stops the writes,
then the contract revision.

The gateway merges first. Between the two merges, nothing that a user sees changes: no organization exists
until the platform administrator makes one, and a console from before the second pull request shows no
entry.

## 18. Alternatives considered

| Question | Not chosen | Why |
|---|---|---|
| Where the organization is stored | The tables `organizations` and `org_members` of revision `0015`, with the kind `team` | They hold one hidden row for each account, the service continues to write them, a trigger of revision `0018` writes the account quota into them, and a contract revision is planned to drop them. New tables keep this work independent of that revision |
| Where the membership comes from | A query on `user_identities` and the `domains` at each use | The role needs a record. The key lookup needs one read through a primary key. And a change of `SSO_PROVIDERS` would change the members of the past |
| One account or two | One account that joins and leaves organizations | #1829 decides that the organization owns the account. Then to remove a member is to suspend the account, and no personal data is in it |
| How usage is counted for an organization | An organization column on each usage row, written with the request | Each write on the request path would change. Sums over the members need no new write, and the time of the membership bounds them (§6.1) |
| Where the limit of a member is kept | In the monthly quota of the account (`account_quotas`), as the first version of this work did | One field would have two owners. An organization administrator could then clear or raise a quota that the platform administrator set. With two fields the lower value applies (§8.1) |
| Where the spend of an organization comes from at admission | The sum of `user_daily_cost` over all members, for each request | The cost grows with the number of members and days. A counter row is one read |
| Where the default limit of a member is kept | A copy in the quota of each member | Each change of the default would write every member, and each account would need a mark that says where its limit came from |
| How a member is kept from a password sign-in | A check at sign-in only, with the password left on the account | Each place that asks "has this account a password" would need the same check: the confirmation of an action, the second factor, the unlink of an identity. Without a password on the account, each of them is correct as it is. The check at sign-in stays for the account that kept a password |
| Where the models of a member are kept | In the preferences of the account, the list that **Admin → Users** sets | One field would have two owners, as for the limit. An organization administrator could then turn on a model that the platform administrator turned off |
| The path of the routes | A new prefix `/organization` | A new public prefix needs an entry in `deploy/public-paths.json`, a route handler and a middleware rule in the console. `/user` has all of them |

## 19. References

- RFC #1829, with its HTML prototype, and RFC #1830
- [GitLab: enterprise users](https://docs.gitlab.com/user/enterprise_user/): an account belongs to a group
  when its primary email is in a verified domain of the group and it meets one more condition, such as a
  SAML or SCIM identity of the group. The member rule of §3 follows this
- [LiteLLM: role-based access controls](https://docs.litellm.ai/docs/proxy/access_control): an organization
  with a budget, and an organization administrator role that cannot change that budget
- [WorkOS: audit logs](https://workos.com/docs/audit-logs): each event belongs to one organization
