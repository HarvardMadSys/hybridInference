# Admin Console: fewer confirmations (2026-10-27)

Supersedes the confirmation rules of
[2026-10-24-admin-console-write-confirmation.md](2026-10-24-admin-console-write-confirmation.md) and
[2026-10-26-admin-console-in-place-editing.md](2026-10-26-admin-console-in-place-editing.md). A save,
a create and most actions now write on the click that asks for them. Unchanged: in-place editing,
the save bar, validation, the locks, the busy guard, partial-save reporting and secret masking. This
record covers what was changed and verified on this date. It is not a design spec.

## The rule now

A confirmation stays only where a mistaken click costs more than the click it saves:

| Kept for | Writes |
|---|---|
| Deleting, or discarding saved values that one click cannot bring back | Delete and Permanently delete (typed) a user, Delete provider (typed), Delete key, Delete an update, Delete a route, Reset a route to config, Reset capabilities to config, Clear offload route, Clear key (Usage Insights), Remove a signup domain, Clear last hour errors, Cancel a batch, Cancel a scheduled broadcast |
| Overwriting many values at once | Apply quota to existing users, Restore configuration from a backup |
| Email to the audience | Send Now / Schedule |
| Dropping requests in flight | Restart backend (Configuration) |
| Input that the dialog collects | Reject (reason), Suspend (message), Delete a user (reason) |
| Unsaved typing it would discard | Discard and edit (Site updates) |

Everything else writes at once:

| Rule | Applies to | Behaviour |
|---|---|---|
| **edit** | Saving in-place changes | **Save changes** writes, with no review. While it runs the button reads "Saving…", and Save, Discard and the fields are held, so a second click writes nothing. A failure shows on the bar, or in the editor or form the save came from. What was not written stays as drafts, and Save again sends only those. A partial save still reads "`<item>` could not be saved: … N changes were saved; the rest are still unsaved." |
| **create** | Add and Create forms | The form's own button creates: Add domain, Add variable, Add key, Add provider, Create model / Add route, Create and publish / Create draft. A failure stays in the form with what was typed, and a typed secret shows as `••••`. |
| **action** | Enable and disable, snooze and resume, mute and unmute, publish and unpublish, approve, reactivate, resume, restore a deleted config route, reset one value (a route weight, a RouteWise parameter), a test to the administrator or to the alert webhook, Run probe, Analyze usage, Download backup | On the click. A failure shows in the section's notice or in a toast. |
| **read-exempt** | Unchanged | |

## Shared building blocks

- **`useConfirmAction`** gains `perform({ key?, run })`, which runs a write at once.
  - **Guard.** It shares the busy guard with `request`: a write is ignored while another runs or a
    confirmation is open, and nothing is queued. `request` is likewise refused while a direct write
    runs.
  - **Result.** It resolves to the failure's message, which the caller shows where the write was
    asked for. It resolves to `null` once the write succeeded, or when it was ignored.
  - **`savingKey`.** It names a direct write as it names a confirmed one, so only the bar being saved
    reads "Saving…". `busy` now covers direct writes too.
  - **Locks.** A lock that waited for an open confirmation also waits for a write in flight now:
    `modelLocked` in `ProviderRoutesTab`, and what `OffloadRoutePanel` and `RoutewiseSettingsPanel`
    report to it.
  - **Tests.** `confirm/useConfirmAction.test.tsx`, "useConfirmAction: direct writes":
    - "writes on the first click, with no dialog, busy and keyed until it ends"
    - "resolves to the failure, so it can be shown and saved again"
    - "opens no confirmation while a direct write runs"
    - "writes nothing directly while a confirmation is open"
- **`writeEach`** now always throws on a failure. Before, it returned the message of a later failure,
  because the dialog closed for a partial save but stayed open for a first failure. Without the
  dialog both are a message on the bar, and the drafts that were written are gone either way, so a
  retry sends only the rest.
- **`EditBar`** gains `held`, for another write on the same write flow: Save and Discard wait for it,
  without reading "Saving…" (see Review follow-up). Otherwise only its documentation changed: Save
  writes, and a failed save's `error` shows on the bar.
- **`ChangeList`, `diffFields`**: kept. Three remaining confirmations list with `ChangeList`: Send
  Now / Schedule, Restore and Reset capabilities to config. Site updates and the provider editor
  count their changes with `diffFields`.

## Changes by screen

The `run` of each converted write is the one its confirmation had, unless a row says otherwise.

### Settings tab

| Write | Now | Notes |
|---|---|---|
| Feature Flags, Numeric Settings (`RuntimeSettingsSection.save`) | direct | Enter in a number saves too. The site config is still refreshed after any write, even a partial one. |
| Add domain (`onAddDomain`) | direct | The form's button reads **Add domain** (it was "Review and add"). A failure shows under the input, and the domain stays typed. |
| Remove domain | kept | Removing the last domain now says that every signup is then approved automatically. The dialog used to say "will require admin approval" there too, which is the opposite of what an empty list does. |
| Apply to existing users | kept | |
| Alert Notifications: snooze, extend, resume | direct | A failure shows in the section's notice. |
| Alert Types: mute, unmute | direct | Choosing a duration mutes. A failure shows in its own dismissible notice, apart from a load error and its Retry. |
| Usage Insights: save | direct | The typed key is still masked in a failure, which now shows on the bar. |
| Usage Insights: Clear key | kept | |
| Model Visibility, Model Concurrency, Batch Concurrency | direct | One request per model, through `writeEach`. Enter in a batch cap saves. |

### Configuration tab

| Write | Now | Notes |
|---|---|---|
| Category save (`saveGroup` → `commitSave`) | direct | `pendingSave` and its review are gone. The inline hints already said what the review did: "Reset when you save", "Deleted when you save", "Becomes secret when you save. This cannot be undone from the console." Reset ordering, failure reporting and masking are unchanged. While any category saves, every category's Save, Discard and Enter wait, as the open review used to hold the page. |
| Send test notification | direct | It changes no setting. Its sanitized result still shows beside the button. It keeps its own in-flight guard. |
| Backup | direct | The button's title says the file holds secrets in plain text, and so does the success toast. A failure is a toast. |
| Restore | kept | |
| Add variable | direct | The form's button reads **Add variable** (it was "Review and add"). A failure shows in the form with a typed secret masked, and the form keeps its values. |

### Users

| Write | Now | Notes |
|---|---|---|
| Account settings: role, daily quota, max concurrent, model access | direct | Still one PATCH, and the quota rules are unchanged: an emptied or negative quota holds Save with its reason, and a deliberate 0 saves. A failure shows on the bar. |
| Admin note, suspension message | direct | A failure shows on the field's own bar; typing or Discard clears it. |
| Approve, Reactivate, Resume | direct | A failure is a toast, as a success is. While a write runs, the rows' Approve and Reject are disabled. The deleted-user line now says what the Resume dialog said: the account's API keys stay revoked. |
| Reject, Suspend, Delete, Permanently delete | kept | Each collects a reason or a message, or the typed identifier. |
| Analyze usage, Re-analyze | direct | The note beside the buttons says what the dialog said: the sample's user-turn text goes to the analysis model, on the Usage Insights key, and the audit log records the run. A failure shows in the panel. |

### Providers

| Write | Now | Notes |
|---|---|---|
| Add key | direct | The form's button reads **Add key** (it was "Review key"). A failure shows in a new line in the form with the typed key masked, and the form keeps its values. |
| Reserved tiers (`saveTiers`) | direct | One request per key. Partial saves report on the bar, and the keys reload once anything was written. |
| Enable, Disable a DB key or an env key | direct | A failure is a toast ("Enable failed: …", the wording from before #1629). |
| Delete key | kept | |
| Provider switch (quota cards, availability table), key switch on a quota card | direct | `providerToggleRequest` is gone. A failure is a toast. |
| Add provider | direct | The probe is still required first, and the button reads **Add provider** (it was "Review provider"). A failure shows in a new line in the form with the key masked, and the fields are held while it saves. |
| Edit provider: save | direct | A new Base URL still needs a probe. The modal now says what the review said: the key and model the probe used are not stored. A failure shows on the bar, and the modal stays open with its drafts. |
| Delete provider | kept | |
| Provider selector, key row actions | | Held while any write runs too, as a key action reloads the provider's keys. |

### Announcements

| Write | Now | Notes |
|---|---|---|
| Create an update | direct | The composer's button says what it does: **Create and publish**, or **Create draft** while Published is unchecked (it was "Create Update"). "Title is required" is still checked first. A failure shows in the composer, and the draft stays. |
| Save an update | direct | Only the changed fields are sent. A failure shows on the editor's bar. |
| Publish, Unpublish | direct | A failure is a toast. |
| Delete, Discard and edit | kept | |
| Row actions | | Edit, Publish/Unpublish and Delete are disabled while a write runs; without the modal a click would have been ignored silently. |
| Send Test to Me | direct | It reaches only the signed-in administrator, whom the button's title names. A failure is a toast. |
| Send Now / Schedule, Cancel a scheduled broadcast | kept | |

### Routing

| Write | Now | Notes |
|---|---|---|
| Route editor: save (`onSubmit`) | direct | The editor closes once saved. A failure keeps it open, with the error on its bar and the drafts kept. |
| Create model, Add provider route (`onCreateSubmit`) | direct | The form had no error area, so a failure now shows in a line above its buttons, and the form stays open with its values. Reopening the form clears it. |
| Capabilities: save | direct | Likewise, a failure shows in a new line in the dialog, and the draft stays. |
| Routing policy, weights, each concurrency limit | direct | Each bar shows its own failure; the policy bar gains `strategyError`. Weights go one request per route, and the offload routes are re-read after any weight write. |
| Disable, Enable, Restore (`setRouteDisabled`, `restoreRoute`) | direct | Each is undone from the same row. A failure is a toast, with the gateway's reason. |
| Weight Reset (`resetWeight`) | direct | One value back to its configured weight. A failure is a toast. |
| Reset config, Delete (runtime and config routes), Reset capabilities to config | kept | Delete still refuses this model's offload route first. |
| Model selector, Create model | | `modelLocked` holds while a write runs, as it held while a confirmation was open. |
| Queue offload: save | direct | All limit validations still refuse before any write. |
| Queue offload: Clear offload route | kept | |
| RouteWise parameters: save | direct | One request per setting; a response for a model no longer shown is still dropped. |
| RouteWise parameters: Reset | direct | One value back to its inherited one. Still disabled while that setting has an unsaved change. A failure is a toast. |
| Run probe | direct | The panel says beforehand that a run sends live requests and counts against the providers' quotas. A failure shows in the probe banner and a toast. |

## Tests

Every test of a removed confirmation now checks four things:
- the write happens on the click, and no dialog opens;
- while it runs, a second click writes nothing;
- a failure shows where the write was asked for, and keeps the drafts or the form;
- trying again writes only what is left.

The tests of the kept confirmations are unchanged, except where a removed one was part of them:
- **Remove domain.** "removes a domain only after a confirmation naming it" now lists two domains, so
  it checks the wording for a domain that is not the last. `__tests__/SettingsTab.roleQuota.test.tsx`
  checks a sentence that both wordings share.
- **Users.** `users/UserTable.test.tsx` "refuses to act on a user who has left the list while the
  dialog was open" goes through Reject, since Approve has no dialog any more.
- **Broadcasts.** The never-replaced guard now starts from a test send in flight (in the table below).

Renamed tests cited by the 2026-10-24 and 2026-10-26 records:

| File | Was | Now |
|---|---|---|
| `__tests__/SettingsTab.siteConfigRefresh.test.tsx` | "edits saved values in place, and changes nothing until the change is confirmed" | "edits saved values in place, and changes nothing until Save" |
| | "refuses an out-of-range number without asking" | "refuses an out-of-range number without writing" |
| | "leaves it alone when the change is refused, keeping the dialog and the draft" | "leaves it alone when the change is refused, keeping the draft and saying why" |
| | "adds a domain only through Add domain and a review" | "adds a domain from the form opened by Add domain, with no review" |
| `AlertSnoozeSection.test.tsx` | "keeps the confirmation open with the error when a snooze fails" | "shows why a snooze failed, leaves the status as it was, and snoozes on a retry" |
| `ConfigurationTab.test.tsx` | "says in the review that the resets follow the value changes as separate requests" | "sends the value changes first, in one request, then each reset on its own" |
| | "masks the secret value of a refused new variable in its review" | "masks the secret value of a refused new variable, and keeps the form to retry" |
| | "sends no test notification when its confirmation is cancelled" | "sends a test on the click, with no confirmation, and saves nothing" |
| `users/UserTable.test.tsx` | "refuses an emptied daily quota instead of reviewing it as 0" | "refuses an emptied daily quota instead of saving it as 0" |
| | "edits role, quota and concurrency in place, writing nothing until Confirm" | "edits role, quota and concurrency in place, writing nothing until Save" |
| | "edits the message in place and saves it only on Confirm" | "edits the message in place and saves it on Save" |
| | "says what an emptied message means, and clears it on Confirm" | "says what an emptied message means, and clears it on Save" |
| `users/UsersTabWrites.test.tsx` | "shows a failed write inside the confirmation instead of a toast" | "approves on the click and toasts a failure, then approves on the next click" and "shows a failed rejection inside its confirmation instead of a toast" |
| `users/UserUsageInsights.test.tsx` | "asks before the paid analysis, and Cancel sends nothing" | "says beside the buttons that the analysis is a paid, audited call" |
| | "runs the analysis once on Confirm and shows the report" | "runs the analysis once on the click, holding both buttons until the report shows" |
| | "keeps the confirmation open with the error when the analysis fails, and retries" | "shows a failed analysis in the panel, and runs it again on the next click" |
| `ProviderKeysTab.test.tsx` | "reviews the new key without its secret and submits the chosen tier on confirm" | "adds the key with the chosen tier on Add key, with no confirmation" |
| | "keeps the add review open with a masked error when adding fails, and retries" | "keeps the form with a masked error when adding fails, and adds the key on a retry" |
| | "reports a partial tier save and retries only the unsaved key" | "reports a partial tier save and saves only the unsaved key again" |
| | "disables a DB key after confirmation" | "disables a DB key on the click, with no confirmation" |
| | "disables an env key after confirmation, and Cancel writes nothing" | "disables an env key on the click, with no confirmation" |
| | "re-enables a disabled env key after confirmation" | "re-enables a disabled env key on the click" |
| `ProviderOverviewTab.test.tsx` | "probes directly, then reviews the new provider without its key before creating it" | "probes directly, then adds the provider on Add provider, with no confirmation" |
| | "keeps the review open with a masked error when creating fails" | "keeps the form with a masked error when creating fails, and adds it on a retry" |
| `__tests__/ProvidersTab.quotas.test.tsx` | "disables a provider only after a confirmation that names it" | "disables a provider on the click, with no confirmation" |
| | "leaves the provider untouched when its confirmation is cancelled" | "leaves the provider as it was when its switch fails, and says why" |
| | "re-enables a provider after confirmation and keeps the dialog on failure" | "says why re-enabling a provider failed, leaves it disabled, and enables it on a retry" |
| | "disables a single key after confirmation and reloads the quotas" | "disables a single key on the click, with no confirmation, and reloads the quotas" |
| | "re-enables a disabled key after its own confirmation" | "re-enables a disabled key on the click" |
| | "keeps the key confirmation open with the error, blocks a second submit, and retries" | "holds the switches while a key switch is in flight, says why it failed, and switches on a retry" |
| | "leaves the key untouched when the confirmation is cancelled" | removed: there is no Cancel, and the in-flight test covers the switches |
| `SiteUpdatesTab.test.tsx` | "reviews what will be published, keeps the form on Back, and creates once on Confirm" | "creates what the form holds, once, on Create and publish" |
| | "keeps the review open with the error when the create fails, and retries" | "keeps the draft with the error when the create fails, and creates it on a retry" |
| | "lists old -> new, keeps drafts on Back, and writes only the changed fields once" | "writes only the changed fields, once, on Save changes" |
| | "confirms an unpublish naming the update; Cancel writes nothing, Confirm writes once" | "unpublishes on the click, once, with no confirmation" |
| | "confirms a publish with where it appears" | "publishes on the click, once, with no confirmation" |
| `BroadcastsTab.test.tsx` | "names the recipient; Cancel sends nothing; Confirm sends one test" | "sends one test on the click, to the recipient the button names" |
| | "keeps an open test-send confirmation when a Send Now count arrives after it" | "opens no send review when a Send Now count lands while a test is sent, and queues none" |
| `ProviderRoutesTab.test.tsx` | "lists the changed fields and saves nothing until the save is confirmed" | "counts the changed fields, writes nothing while typing, and saves once on Save" |
| | "edits fixed route weights in place and confirms each change" | "edits fixed route weights in place, saves each change on Save, and resets one on its click" |
| | "updates a runtime OpenRouter concurrency limit in place after confirmation" | "updates a runtime OpenRouter concurrency limit in place on Save" |
| | "refuses a zero concurrency limit before the review" | "refuses a zero concurrency limit before any write" |
| | "refuses a negative weight before the review" | "refuses a negative weight before any write" |
| | "refuses an invalid concurrency limit before the review" | "refuses an invalid concurrency limit before any write" |
| | "reviews what a new route will add, keeps the form on Back, and adds it once" | "adds a new route once on Add, with the capabilities it declares, and no review" |
| | "reviews a new model before creating it and keeps a failed create retryable" | "keeps the create form and its values when a create fails, and creates once on a retry" |
| | "asks before disabling a route, and Cancel writes nothing" | "disables a route on its click, holding the row actions until the write ends" |
| | "edits a route from what it serves now, writing only after the review" | "edits a route from what it serves now, writing only on Save" |
| | "blocks a second Confirm and closing while a capability save is in flight" | "holds a second Save, closing and the fields while a capability save is in flight" |
| | "holds the model selection while an offload draft or its review is open" | "holds the model selection while an offload draft is unsaved, after a failed save too" |
| | "releases the model selection once an offload save is confirmed" | "holds the model selection while an offload save runs, and releases it once saved" |
| `OffloadRoutePanel.test.tsx` | "reports only unsaved changes or an open confirmation to the parent" | "reports unsaved changes, an open confirmation or a write in flight to the parent" |
| `RoutewiseSettingsPanel.test.tsx` | "reports only unsaved changes or an open confirmation to the parent" | "reports only unsaved changes or a write in flight to the parent" |
| | "resets only an explicit override, after confirmation, and shows the inherited source" | "resets only an explicit override, once, on the click, and shows the inherited source" |
| | "asks before sending live probe traffic and names the endpoint" | "runs a probe for the chosen endpoint on the click, with no confirmation" |
| | "keeps a failed probe run in the dialog so it can be retried" | "shows why a probe run failed, and runs it again on the next click" |

New tests, besides the four of the hook:
- **Settings:**
  - "saves a number on Enter, once, and holds the section while it saves"
  - "keeps the typed domain and says why when adding it fails, and adds it on a retry"
  - "says that removing the last domain approves every signup"
  - "saves on Enter in a cap, once, and holds the caps while it saves"
  - "holds Save, Discard and the boxes while a save is in flight"
  - "holds Save, Discard and the fields while the save is in flight" (Usage Insights)
- **Configuration:** "says why a backup could not be downloaded, and allows another try".
- **Users:**
  - "shows a failed message save beside its bar, keeps the draft, and saves it again"
  - "holds Approve and Reject while an approval runs"
  - "toasts a failed approval, and approves on the next click"
  - "keeps the panel open and toasts a failed reactivation"
- **Providers:**
  - "holds the form while the key is being added, and adds it once"
  - "holds the provider and every key action while a key action is in flight"
  - "says why a key action failed, and the same action can be tried again"
  - "holds the form while the provider is being added, and adds it once"
  - "masks the probe key in a failed save of a new Base URL"
  - "holds the switches while a provider switch is in flight, and switches once"
- **Announcements:**
  - "holds the composer, a second create and the row actions while the create is in flight"
  - "holds every row action while a publish is in flight, so a second click writes nothing"
  - "holds the sends while a test is sent, and a second click sends nothing"
- **Routing:**
  - "starts a reopened create form without the last failure"
  - "says why a reset failed and keeps the override, which can be reset again"

## Review follow-up

An independent review found that the confirmation's backdrop had a second job. While a confirmed
write ran, the backdrop covered the page, and four places relied on that. Each is fixed, and each
has a test. The tests for 1 and 3 were run against the code without their fix, and fail there.

1. **Users: opening another row during a save.**
   - **The bug.** The first user's refreshed details could land on the second user's panel, and a
     later save from that panel would be computed against them.
   - **The fix.** Rows wait while a write runs (`toggleDetail`). A refreshed detail is applied only to
     the same user.
   - **Test.** `users/UserTable.test.tsx`: "keeps the open user while its save runs, and opens another
     one only after".
2. **Routing dialogs, Add variable: fields during the write.**
   - **The bug.** The route editor, the create form and the capabilities dialog kept their fields
     editable while their write ran. They close once it succeeds, so anything typed meanwhile was
     dropped.
   - **The fix.** Their fields are held while it runs, and so are Add variable's.
     `RouteCapabilityFields` and `RouteDeploymentField` gain `disabled` for this.
   - **Tests.**
     - `ProviderRoutesTab.test.tsx`: "holds a second Save, closing and the fields while a route save
       is in flight", "holds the add form, its fields and closing while the route is being added",
       "holds a second Save, closing and the fields while a capability save is in flight"
     - `ConfigurationTab.test.tsx`: "holds the fields of a new variable while it is added, and adds it
       once"
3. **Save bars that share a write flow.**
   - **The bug.** A bar read "Saving…" only for its own write, but stayed enabled during another write
     on the same flow. `perform` ignores a write started then, so the click did nothing.
   - **The fix.** Such a bar is now `held`:
     - Routing: the policy, the weights, each limit;
     - provider key tiers;
     - the Site updates editor;
     - the Users account settings, note and message.

     The weight Reset and the scheduled-broadcast Cancel wait in the same way.
   - **Tests.**
     - `confirm/EditBar.test.tsx`
     - `ProviderRoutesTab.test.tsx`: "holds the weights bar while another route write runs, then saves
       the drafts"
     - `ProviderKeysTab.test.tsx`: "holds the tier bar while a key action runs, then saves the tier"
     - `SiteUpdatesTab.test.tsx`: "holds the editor bar while another update is published, then saves
       the edit"
     - `BroadcastsTab.test.tsx`: "holds Cancel while a test is sent, then opens its confirmation"
4. **Routing: editors opened during another write.**
   - **The bug.** An editor opened then read "Saving…" for a write that was not its own, and a policy
     save closed it.
   - **The fix.** Edit, Capabilities and Add provider wait while a write runs. The weight Reset reads
     "Resetting…" while it runs.
   - **Test.** "disables a route on its click, holding the row actions until the write ends", which now
     also checks Edit, Capabilities and Add provider.

## Verification

Run from `apps/frontend`:

| Command | Result |
|---|---|
| `npm run format:check` | clean |
| `npm run lint` | clean |
| `npm run type-check` | clean |
| `npm run i18n:check` | clean |
| `npm test` | 155 files, 1,991 tests passed |

Not done: browser acceptance against a running deployment.
