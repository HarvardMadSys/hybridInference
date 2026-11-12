# Alert noise reduction (2026-11-04)

Status: agreed. Rollout step 1 (§8) is implemented next to this document,
switched off by default. Baseline: `dev` at `e1d733421`.

Scope: the backend gateway alert path, `alert_slack()` in
`apps/backend/serving/observability/alerts.py`. The status-monitor path, owned
by the freeInference repository, is out of scope. Under issue #1103 the two
paths stay independent, and this design does not connect them.

## 1. Summary

The alert channel is so noisy that real alerts get lost in it. On 10-25 the
production gateway was down for six and a half hours. The first card arrived
four minutes in, and nobody noticed it until about six hours later.

This design does one thing: it cuts what reaches the channel until everything
left there is worth reading. Problems nobody has time to fix are not dropped.
They go into one daily list that people can work through when they have time.

There are four changes:

1. **Two destinations.** The channel gets only two kinds of problem: ones users
   are feeling right now, and ones that will become serious if nobody fixes
   them. Everything else is recorded in alert history and counted in the digest.
2. **One message per problem.** A problem that is still open, or that recurs
   within 24 hours, does not post again. The only exception is when it gets
   bigger.
3. **Per-problem mute.** An admin can silence one known problem, such as one
   provider's exhausted quota, without muting every alert of that type.
4. **A daily digest.** Every day, one message lists the open problems by impact
   and age, plus everything that is currently muted.

## 2. Goals and non-goals

Goals:

- Every message in the channel is worth reading.
- No problem stays open for more than a day without appearing somewhere a
  person will see it, which is the digest.
- Use only what already exists: `alert_slack()`, alert history, the admin
  mutes and `site_settings`.

Non-goals:

- **No response process.** There is no acknowledgement, claiming, owners,
  on-call rota, phone paging or escalation when nobody responds. The team does
  not have the capacity to promise a response time. Problems are fixed when
  someone has time, or right away when one is clearly serious.
- **No new alerting infrastructure.** Prometheus and Alertmanager were removed
  in #365 and #399, and they stay removed. Backend producers are not routed
  through the alert control plane (#1103).
- **No separate staging channel.** Staging and production keep sharing one
  channel and follow the same rules. Staging gets quieter for the same reasons
  production does. Splitting would only take a webhook change if it is ever
  wanted.

## 3. Evidence

The evidence comes from the FreeInference deployment. The sources are:

- a full read of the alert channel over 90 days, from 2026-07-21 to 10-19;
- the channel and production alert history from 2026-10-28 to 11-03;
- a production outage probe.

All production reads were read-only.

| Observation | Numbers |
|---|---|
| Channel volume over 90 days | About 60 bot messages a day. Still 78 a day after per-type mutes arrived on 09-28. |
| One cause, many messages | 2,471 circuit-open cards plus 670 recoveries came from 652 distinct episodes. One revoked key produced 107 messages over two days. |
| Most problems heal on their own | The median circuit episode recovered in 7 minutes, and 34% recovered within 2 minutes. |
| The real outage was missed | 10-25: the backend ran out of file descriptors and production was down 06:43–13:13 UTC. The first card ("Too many open files") came at +4 min. The status-monitor CRITICAL came at +17 min. A maintainer noticed at about +6h20m, with staging cards interleaved in the channel. |
| Long problems were invisible | One provider's quota was exhausted (HTTP 429, code 1310), which kept three routes' circuits open for days. All of it was suppressed as `policy_disabled` because `circuit_open.page_on_usage_limit` is `false`. In all, five routes stayed down for more than a day without a single message. |
| Flapping repeats | One route produced about 30 cards in six days for the same problem. Staging posted about 640 cards in a day and a half, mostly repeated "Recovered" cards for one local model (before #1712). |
| Client errors counted as failures | 11-03 23:24 UTC: the DB detector fired at 32% failures. Part of that was upstream 400 "function name is invalid", which is the caller's own mistake. |
| Mutes are forgotten | `auth_failure_spike`, `auth_ip_blocked` and `cost_overrun` are muted with no end date. `cost_overrun` alone was suppressed 91 times in 13 hours. Nothing reminds anyone that these mutes are still on. |
| Current production volume | 72 gateway posts from 10-28 to 11-03, matching the 72 `delivery_attempt` rows in alert history. |

There are caveats:

- `api_logs` failure rows can be deleted after the fact, by
  `delete_recent_error_requests` and by user erasure. Some rows from the 10-30
  incident are already gone, so replays over `api_logs` undercount failures.
- The estimates in §8 are rough. They are not a guarantee.

## 4. Why it is noisy

1. **Each flap is a new episode.** In-process episode state opens and closes
   with every flap and resets on restart. A route that flaps 13 times a day
   posts 13 times, or 26 if recoveries are counted.
2. **Events that fix themselves are posted.** A circuit that reopens in two
   minutes, a short 503 burst, a scanner, a latency bump: none of these need a
   person.
3. **Three detectors report the same thing, and their definition of "failure"
   is wrong.** The three are:
   - `failed_request_rate` (deployment-wide, in-process);
   - `fivexx_rate` (deployment-wide, in-process);
   - `failed_request_rate_db` (deployment-wide, over `api_logs`).

   None of them looks at a single model. The DB detector counts client-caused
   4xx rows whenever an error is logged. An outage that hits one model, or one
   that also cuts traffic, can stay under all three.
4. **Mutes cover a whole type.** The only way to quiet one known problem is to
   mute its whole type. When `circuit_open` was muted, every other route's
   outage went silent with it. Mutes with no end date are never shown again.

## 5. Design

### 5.1 Two destinations

The channel gets a card only when users are failing now, or when a problem will
become serious if left alone. Everything else is still recorded in alert
history, exactly as today, and counted in the digest. Record-only decisions
fall back to the channel if the history queue cannot accept them.

Proposed mapping of the current alert types:

| Alert type | Destination | Notes |
|---|---|---|
| `failed_request_rate_db` | Channel | Becomes the single failure detector (§5.2). |
| `failed_request_rate` | Record only | Replaced by §5.2. |
| `fivexx_rate` | Record only | Replaced by §5.2. |
| `stream_failure_rate` | Record only | Covered by §5.2: a stream that returned 200 and then failed counts as a failure there. |
| `circuit_open` | Channel when the circuit stays open (§5.3), otherwise record only | Covers outages, revoked keys and exhausted quotas. |
| `key_pool_exhaustion_burst` | Digest | When it matters, it shows up as a circuit that stays open. |
| `upstream_auth` | Channel | A rejected gateway credential always needs a person. |
| `db_disconnect` | Channel | |
| `provider_spend` | Channel | Real money. |
| `request_log_queue_saturation` | Digest | Report rows are lost, but no user request fails. |
| `tracked_task_failure` | Digest | |
| `prefix_cache_pending_leak` | Digest | |
| `client_error_burst` | Digest | |
| `p95_latency` | Record only | |
| `auth_failure_spike`, `auth_ip_blocked` | Record only | These are mostly scanners. |
| `cost_overrun` | Record only | One user reaching their own quota. |

Notes on the table:

- "Digest" means the type is listed in the daily digest while it is open, but
  never posted to the channel.
- "Record only" types appear in the digest only as a count.
- The destination is configured per type in `alerts.yaml`, next to the existing
  thresholds. It is not hard-coded.

### 5.2 One failure detector, per model

`failed_request_rate_db` becomes the single failure detector:

- It counts each client request once, by its **final outcome** as the client
  saw it. A retry that eventually succeeded is not a failure. A stream that
  returned 200 and then broke is a failure.
- It excludes 4xx client errors, 429, 499 (the client cancelled) and
  model-not-found.
- It groups by `model_id`, and also evaluates the deployment as a whole.
- It fires when the failure rate in a window reaches a threshold, with a
  minimum request count so that a model with almost no traffic cannot trip it
  on a couple of errors.
- Replayed over 10-28 to 11-03, a starting point of **≥ 20% over 10 minutes
  with at least 20 requests** produced 7 episodes and 4 messages, given the
  §3 caveat about deleted rows.
- Deployment-wide failure is a separate, larger key. It has the same rule and
  is what catches an outage like 10-25.
- It runs on one worker at a time, under a `pg_try_advisory_lock`. That is the
  same pattern as `provider_stats_rollup.py`.

The two in-process detectors keep running and recording, so their history
remains available for comparison. They stop posting.

### 5.3 Circuits post only when they stay open

A `circuit_open` episode posts only once the circuit has been open, or
flapping, for longer than a configured duration (`min_open_sec`, 2 hours by
default). Flapping counts: the open time of every stretch inside the 24-hour
window (§5.4) adds up.

The first draft also required that the model had no healthy route left, or that
users got failures for it. That condition is left out: replaying production
(§8) shows the duration alone already posts each long outage once, and a model
whose users fail is reported by §5.2 anyway.

This holds whatever the cause: an outage, a revoked key or an exhausted quota.
It replaces `page_on_usage_limit: false`, which today silences an exhausted
quota forever, and makes type-muting `circuit_open` unnecessary.

### 5.4 One message per problem

- The notification key is the existing alert key (`dedupe_key`), for example
  `circuit_open:<endpoint_id>` or `failed_request_rate_db:<model_id>`.
- A key posts once when it first meets its channel rule. It does not post again
  while the problem is open.
- If the problem recurs within **24 hours** of recovering, it reopens the same
  problem. Nothing is posted, and the digest shows the recurrence count.
- Two cases post again:
  1. **The problem got bigger.** A per-model failure becomes deployment-wide,
     or one route becomes every route of a model.
  2. **A deployment-wide failure recurs** after it recovered. This is always
     posted, because it is the 10-25 case. In configuration this is the
     `renotify_on_recurrence` list of keys (a trailing `*` matches a prefix).
- A recovery card is posted only for a problem that had a channel card. Flaps
  inside the 24-hour window are not posted.
- The environment label moves to the front of the title, for example
  `[staging] 5xx rate exceeded`. That way staging and production can be told
  apart at a glance in the shared channel.

### 5.5 Muting one problem

The admin console's Alert Types list gains a per-problem mute next to the
existing per-type mute. For example, it can mute
`circuit_open:glm-5.3:zai-api` for 7 days.

- It is stored like type mutes: one `site_settings` row per key
  (`slack_alert_key_mute:<key>`) with an `until` value. It is cached the same
  way, so every worker sees it.
- A muted problem still appears in the digest, marked as muted, so it is not
  forgotten. Other problems of the same type still post.
- In history it is recorded as a suppression decision with a new
  `key_muted` reason.

The console can show open problems taken from alert history, each with a "Mute
for…" control. This is the only new console surface.

### 5.6 Daily digest

One message a day, at a configured hour (for example 09:00 deployment-local),
built from `alert_history_events`:

1. **Open problems**, worst first. For each: the key, what it is, when it
   started (age in days), affected users and failed requests since it started,
   recurrences, and whether it is muted.
2. **Resolved since the last digest**: a count per type, plus the longest
   problem.
3. **Record-only activity**: one line of counts per type, for example
   `cost_overrun 91 · auth_ip_blocked 4`.
4. **Active mutes**: every type mute and per-problem mute, with when it was
   set and when it ends ("no end date" stated plainly).

Delivery details:

- One worker sends it, under a `pg_try_advisory_lock`. A sent marker in
  `site_settings` stops a restart from sending it twice.
- It goes through the same `alert_slack()` sink and is recorded in history like
  any other post.
- It honours the global snooze. A snooze is at most 24 hours, so this skips one
  digest at most.
- A digest that stops arriving shows that gateway alerting itself is broken.
  This is the only health check the design adds.

## 6. Relation to the current admin settings

Nothing is removed and no setting changes meaning.

| Current setting | Effect |
|---|---|
| Global snooze (30 min – 24 h) | Unchanged. It also pauses the digest. |
| Per-type mute (1 h, 24 h, 7 d, or until unmuted) | Unchanged. It keeps working alongside per-problem mute. |
| Notification channel and webhook, test message | Unchanged. There is one channel. |
| Types that become "record only" | Still listed, labelled "Record only, not posted". Their mute control is disabled, because muting them would do nothing. |

Thresholds, durations and destinations stay in `alerts.yaml`, as they are
today. They do not move into the console.

## 7. Multiple workers and restarts

The design does not assume one gateway process.

| State | Where it lives | Behaviour with N workers or after a restart |
|---|---|---|
| Type mute, per-problem mute, snooze | `site_settings`, cached | Shared by all workers. Survives restarts. |
| Unified failure detector | Reads `api_logs`, runs under an advisory lock | Runs once across workers, so all workers agree. |
| Digest | Advisory lock plus a sent marker | Sent once. |
| "Already posted" and the 24-hour reopen window | In process, as today | At startup each process reads the keys this deployment posted in the last 24 hours from alert history, so a restart or a new worker does not announce them again. After that, workers do not see each other's posts. |

If alert history cannot be read within 3 seconds, the process starts empty and
posts. A possible duplicate is better than a lost alert.

## 8. Rollout

Each step can be reverted on its own.

1. **Notification policy** (§5.1, §5.3, §5.4). Implemented as
   `notification_policy` in `alerts.yaml`, off by default. Alert history
   already records every decision the gateway made, so these rules are checked
   by replaying it (`ops/db/analysis/alert_policy_replay.py`) instead of a
   shadow week. Replaying production from 10-28 to 11-05 gives 57 messages
   instead of 95; 44 of the 57 come from the old deployment-wide detector,
   which step 2 replaces. Production turns the policy on after step 2.
2. **Per-model detector** (§5.2). It has no history to replay, so it runs one
   week in record-only mode next to the current rules.
   - Compare: did it have a card for every problem a maintainer acted on, and
     for every outage the probe saw?
   - Also run fault drills on staging: stop a local model, revoke a key, and
     force a deployment-wide error.
   - Then switch it on. The replaced detectors (§5.1) become record only.
3. **Per-problem mute** (§5.5), including the console control.
4. **Daily digest** (§5.6).
5. **Optional: resource checks.** File descriptors and disk below a threshold
   go to the channel. 10-25 was a file-descriptor outage, and this check is
   cheap.

**Estimate.** Replaying production from 10-28 to 11-03 under these rules gives
fewer than 10 channel messages instead of 72, before staging. This is a rough
replay. "A few messages a week" is something to watch for, not a quota.

**Success measures**, checked after a month:

- Channel messages per week.
- The share of channel messages someone acted on, or would have.
- Real outages without a channel card. The target is none.
- Open problems older than a day that did not appear in the digest. The target
  is none.

## 9. Open questions

1. The starting thresholds. The proposal is ≥ 20% / 10 min / ≥ 20 requests,
   and 2 hours for circuits. The replay and the detector's record-only week
   should settle them.
2. Whether "Recovered" cards are wanted at all, or only for deployment-wide
   problems.
3. Whether the status monitor should export its history (built, but off by
   default) so that the digest can include its problems too. That would be a
   change in the freeInference repository.
