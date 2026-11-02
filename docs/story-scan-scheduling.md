# Bounded story scan scheduling

An active catalog can consume a profile's entire 15-request budget. Previously,
completing that catalog cleared its progress before the pending archive ran, so
the next poll could repeat the same catalog indefinitely. The scheduler now
reserves an archive turn and retains each completed source while other started
scans catch up. Every poll still validates the source's current head; target,
login, catalog, or relevant head changes invalidate cached completion. Unresolved
active evidence on archive pages remains partial until revalidated.

A second case involves a retained-post scan already in progress. Finding a new
active/life story must not short-circuit that pending traversal or falsely report
overall completion. Known post continuations now join the returned story result
within their existing request budget, with author/story filtering, deduplication,
and normal cancellation/login handling.

Windows review compared the actual public v1.2.10 implementation with this code
on the same synthetic 104-active-item plus one-archive-item workload, reloading
saved JSON between polls:

| Ten-poll replay | v1.2.10 | Updated scheduler |
| --- | ---: | ---: |
| Completed scans | 0 | 5 |
| Unique items recovered | 104 | 105 |
| Archive item recovered | No | Yes |
| Total mobile requests | 190 | 110 |

The test suite also runs 100-poll scheduling replays and covers changing heads,
large catalogs, independent source completion, lost/legacy checkpoints, failure,
cancellation, login scope, and unresolved active buckets. These are controlled
transport tests, not claims about a live account having 104 active stories.

The original PR's Windows CI failed because a fixture executed `git show` on
a development-only commit absent from the repository. Migration coverage now
uses explicit serialized checkpoint data and exercises fresh and resumed scans;
it does not require Git history or an external deployment checkout.

## Archive and refresh modes (issue #19)

The original `story/feed` author-history request uses `to_uid`, `offset`,
`count`, `story_ttl=0` and empty insert/delete IDs. Refresh parameters select
a different view and can return only the current story while omitting archived
ones. Query the original archive mode first. If its head is empty, unfinished,
and has no usable offset or unresolved active bucket, try the refresh mode
within the same 15-request history budget. A nonempty/failed/pending archive
scan remains partial rather than being replaced by a completed refresh.

Both modes validate their current heads and merge by item ID with author
checks. Archive state uses the original endpoint key; refresh state uses the
`#refresh` key. Old path-only refresh checkpoints migrate to that key once,
so an old cursor or completed head cannot short-circuit archive retrieval.
A complete archive traversal supersedes optional refresh progress. Target and
login changes still reset the entire scope.

Parameter-sensitive tests cover one active plus two old archived stories,
refresh-only accounts, JSON reloads, old checkpoint migration, cursor separation,
partial nonempty archives, cancellation and login errors. The previous live
40-story catalog regression remains in the suite.
