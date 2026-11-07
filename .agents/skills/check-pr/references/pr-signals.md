# Reading PR signals

Run queries in the intended repository, or pass an explicit `--repo`. Replace
`<N>`, `<owner>` and `<repo>` with the selected PR and repository. Capture the
head SHA and re-check it after pushes and before any authorized merge.

## PR and checks

```bash
gh pr view <N> --json number,url,state,isDraft,baseRefName,headRefName,headRefOid,headRepositoryOwner,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,comments,latestReviews
gh pr diff <N>
gh pr checks <N> --json name,state,bucket,link,workflow
gh pr checks <N> --required --json name,state,bucket,link,workflow
```

`reviewThreads` is not a supported `gh pr view --json` field. Use the paginated
GraphQL query below (or a connector with pagination) to read unresolved threads.
`comments` and `latestReviews` alone do not establish that all threads are resolved.

Interpret structured results rather than grepping CLI text for "fail":

- `bucket` distinguishes `pass`, `fail`, `pending`, `skipping` and `cancel`.
  Only a successful completed result proves a check passed. Investigate skipped
  or neutral required checks in the context of branch policy.
- `gh pr checks` exits with code 8 when checks are pending. Other query errors
  must be surfaced; they do not mean checks passed.
- No checks may mean workflows have not started, path filters excluded them,
  or no checks are configured. Verify which case applies before claiming green.
  An empty required-check list is not a query failure or proof of readiness.
- `statusCheckRollup` belongs to the fetched head; re-query after a push. Do not
  use green checks from a previous revision to validate a new commit.
- `REVIEW_REQUIRED`, `CHANGES_REQUESTED`, an unknown mergeability state or an
  API failure requires more evidence before claiming readiness. Do not bypass
  required approvals or treat the absence of an approval rule as an approval.

For Actions failures, obtain run/job IDs from the check's actual details URL or
Actions API. Use `gh run view <run-id> --log-failed` (or the specific job logs).
A check-run ID is not an Actions run ID. For external providers, read the linked
provider diagnostics instead.

## All review threads

```bash
gh api graphql --paginate -F owner='<owner>' -F name='<repo>' -F number=<N> \
  -f query='
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes {
          id isResolved isOutdated path line
          comments(first: 100) {
            nodes { id body url author { login } }
            commentsPageInfo: pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'
```

The CLI paginates the outer thread connection. The nested page info is aliased
as `commentsPageInfo` so `gh api --paginate` uses only the outer `pageInfo` for
its cursor. Check each comments connection's `commentsPageInfo.hasNextPage` too;
if true, fetch the remaining comments for that
thread with `node(id: <thread-id>) { ... on PullRequestReviewThread { comments(...) } }`.
Outdated threads still need evaluation; they are not automatically resolved.

When authorized to update review threads, resolve only genuinely addressed
issues using the GraphQL mutation below. There is no REST endpoint at
`/pulls/<N>/threads/<id>/resolve`.

```bash
gh api graphql -F threadId='<thread-node-id>' -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}'
```

Use structured arguments or body files for review text; avoid shell-interpolated
multiline bodies. Preserve unresolved disagreements for the user.
