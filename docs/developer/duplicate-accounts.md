# Duplicate accounts

The duplicate-account scan finds accounts that may belong to one person: several sign-ups from one address within minutes, accounts used from one address in the same hours, one browser signed in to several accounts. It is for administrators of a deployment that gives each account its own quota. Run it under **Users → Duplicate accounts** in the admin console, or through the [admin API](#admin-api).

The scan only reports. It changes no account, and each group it lists comes with the evidence behind it, so a person can decide what, if anything, to do.

## Running a scan

Choose a window (the last day up to the last 90 days, 14 by default) and press **Scan**. The result lists groups of accounts, highest score first. Each group shows its members, oldest account first, with their status, role, requests in the window and sign-up date; a member's name opens the account below. **Why these accounts** lists every linked pair and what links it.

A scan reads every request the window holds, so it runs only when you ask, and a long window on a busy gateway takes longer. If it cannot finish within 50 seconds, counting any wait for another scan to finish, it stops and asks for a shorter window. A gateway process runs one scan at a time. For half a minute after a window is scanned, asking for that window again, with any `min_score` or `limit`, reuses the scan.

## The evidence

The scan links two accounts by what they share in the window. Each kind of evidence has a weight:

| Evidence | Weight | Two accounts that |
|---|---|---|
| `same_browser` | 8 | are signed in together on one browser of the console |
| `email_alias` | 6 | have email addresses that reach one mailbox |
| `signup_burst` | 3 | registered from one address within 60 minutes of each other |
| `signup_ip` | 3 | registered from one address |
| `login_ip` | 3 | both signed in from one address |
| `cross_ip` | 3 | split one address: one sent API requests from it, the other registered or signed in from it |
| `co_active` | 3 | sent requests from one address in the same clock hour, in at least 2 different hours |
| `request_ip` | 2 | both sent at least 2 API requests from one address |
| `activity_profile` | 2 | are active at the same hours of the week (cosine similarity of at least 0.9, with at least 10 active hours each) |
| `same_client` | 2 | used one uncommon client: a User-Agent at most 3 accounts used in the window, with at least 2 requests each |

An address is an IPv4 address or an IPv6 /64, because a host's temporary IPv6 addresses change inside its /64. Registrations are self-service sign-ups by email, phone or single sign-on; an account an administrator created is not one. Sign-ins are successful ones only.

Email addresses reach one mailbox when they match after dropping everything from a `+` in the part before the `@`, for every provider, and for Gmail also the dots there (`googlemail.com` counts as `gmail.com`). So `jane.doe+test@example.com` and `jane.doe@example.com` match, and at Gmail `jane.doe` and `janedoe` are one mailbox too.

`activity_profile` and `same_client` are compared only between accounts that some other evidence already links, so behaviour alone never links two accounts. They only strengthen a link found another way.

## From links to groups

Each kind of evidence counts once per pair, however many addresses carry it: sharing six addresses of one provider's pool is one fact about the pair, not six. Two accounts join one group when the evidence between that pair reaches 5. Every kind drawn on an address weighs less than that, so no single kind of address evidence links two accounts. Two kinds do, even on one address: registering close together, being active in the same hours, or both signing in and sending requests from it. Two people behind one home or office router who both sign in and use the API, or use it in the same hours, therefore form a group, at the lowest score. A group with no evidence but shared addresses is marked **Shared addresses only** in the console and `address_only` in the API. Using an address in the same hours counts as sharing it; registering from it close together does not, and neither does identity or behaviour evidence.

Because each merge needs strong evidence between a pair, a chain of weak links cannot pull strangers into one group. A group's score is the sum of the weights of every linked pair in it, so larger and more tightly linked groups score higher. The score and **Why these accounts** cover every linked pair in the group, including pairs too weak to have merged on their own.

A group of more than 10 accounts is usually a shared network rather than one person. The scan raises the bar inside such a group until its parts are plausible and reports the parts that remain. Whatever stays that large is listed apart, under the components linked too loosely to be one person's, so that the limit is visible rather than silent. A group that same-mailbox or same-browser evidence holds together is reported whole, however large: that is one person with many accounts.

## Shared networks and the operator's own accounts

An office, a campus, a VPN exit or a mobile carrier puts unrelated people on one address. An address that more than 5 accounts used in the window links nobody, and the result says how many addresses it set aside. A browser holding more than 10 accounts is treated as a shared computer in the same way.

Administrator, internal and service accounts are never linked: they share the operator's own addresses by design. They still count towards an address's 5 accounts, so an address the operator's staff use is treated as shared.

Requests that the gateway or another service sends on an account's behalf say nothing about where the person is, so the scan skips them: batch work, cloud-agent sandboxes and synthetic probes. An address the gateway could not attribute to a client (see [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md)) is ignored. Private addresses count, because the gateway records one only for a network authorized as direct clients.

## The window

Registrations, sign-ins and requests count only inside the window. A browser or a mailbox is not bound to it, but links accounts only when one of them was active in it; a dormant account that shares a mailbox with an active one is still reported. Accounts stay signed in to a browser until they sign out of it, so `same_browser` reflects the browsers that hold the accounts now.

## Caveats

- A household, a lab or a small office shares an address, and its members may sign up on the same day. Read the evidence before acting on a group.
- The console lets one person sign in to several accounts on purpose. `same_browser` shows that one browser holds the accounts, not that the deployment's rules were broken.
- The scan reads where and when accounts registered, signed in and sent requests. A deployment that runs it should say so in its privacy policy, and should use it as input to a person's decision, not as grounds for automated enforcement.

## Admin API

`GET /admin/users/duplicate-accounts` needs admin authentication (a console session or `ADMIN_TOKEN`).

| Parameter | Default | Meaning |
|---|---|---|
| `days` | 14 | The window, 1 to 90 days |
| `min_score` | 5 | Leave out groups scoring below this, 1 to 100 |
| `limit` | 100 | List at most this many groups, 1 to 500 |

The response has `accounts_scanned` (the accounts active in the window, the operator's own aside), `shared_addresses` and `shared_browsers` (how many were set aside as shared), `total_groups`, `groups` and `oversized`. Each group has its `score`, `user_ids`, `signals` (the kinds of evidence found, strongest first), `address_only`, `members` and `links`. Each link names two accounts and lists its `evidence`: the `kind`, its `weight`, the `addresses` it was seen on (empty for identity and behaviour evidence) and a one-sentence `detail`. A scan that runs out of time is answered with 504.
