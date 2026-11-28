# Reading PR signals

Run queries in the intended repository, or pass `--repo`. Replace `<N>`,
`<owner>` and `<repo>` with the PR and repository. Record the head SHA, and
check it again after every push and before any authorized merge.

## PR and checks

```bash
gh pr view <N> --json number,url,state,isDraft,baseRefName,headRefName,headRefOid,headRepositoryOwner,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,comments,latestReviews
gh pr diff <N>
gh pr checks <N> --json name,state,bucket,link,workflow
gh pr checks <N> --required --json name,state,bucket,link,workflow
```

Read the structured results; do not grep CLI text for "fail".

- `bucket` is one of `pass`, `fail`, `pending`, `skipping` and `cancel`. Only a
  successfully completed check has passed. Investigate skipped or neutral
  required checks against branch policy.
- `gh pr checks` exits with code 8 while checks are pending. Report any other
  query error; it does not mean the checks passed.
- No checks can mean the workflows have not started, path filters excluded
  them, or none are configured; find out which before claiming green. An empty
  required-check list is neither a query failure nor proof of readiness.
- `statusCheckRollup` belongs to the fetched head. Re-query after a push, and
  never use a previous revision's green checks for a new commit.
- `REVIEW_REQUIRED`, `CHANGES_REQUESTED`, an unknown mergeability state or an
  API failure needs more evidence before you claim readiness. Never bypass
  required approvals, or treat a missing approval rule as an approval.

For an Actions failure, take the run or job ID from the check's details URL or
the Actions API, then run `gh run view <run-id> --log-failed` (or read that
job's logs). A check-run ID is not an Actions run ID. For an external provider,
read its linked diagnostics.

## All review threads

`gh pr view --json` has no `reviewThreads` field, and `comments` and
`latestReviews` cannot show that every thread is resolved. Read the threads
with this paginated GraphQL query (or a connector that paginates):

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

`gh api --paginate` follows only the outer `pageInfo`; the nested one is aliased
`commentsPageInfo` so it does not interfere. When a thread's
`commentsPageInfo.hasNextPage` is true, fetch the rest of its comments with
`node(id: <thread-id>) { ... on PullRequestReviewThread { comments(...) } }`.
Outdated threads still need evaluation; they are not resolved.

When authorized to update threads, resolve only issues that are actually fixed,
with this mutation (there is no REST endpoint for it):

```bash
gh api graphql -F threadId='<thread-node-id>' -f query='
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}'
```

Pass review text as structured arguments or body files, not shell-interpolated
multiline strings. Leave unresolved disagreements for the user.
