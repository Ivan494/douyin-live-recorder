# Story pagination verification, 2026-10-30

The live profile response returned 20 media objects and an explicit catalog of
40 story IDs, with `active_data.has_more=true`. Increasing `active_data_size`
to 40 returned the same 20 objects. This reproduces the limitation reported in
issue #13; enlarging the window alone does not resolve it.

Static inspection of the official Android 40.9.1 package identified the read
path in `Story25FeedApi` and its caller. The APK came from the redirect of the
mobile download link on Douyin's official website. Its SHA-256 was
`502e25742bb38cc6d2a6a13bf01eaf4ddae332fbf118e74c9598fb9c1dac0f24`.

The caller uses GET `/aweme/v2/new/story/feed/`, `to_uid`, `direct=0`
(`LOAD_MORE`), the desired catalog item's cursor plus one, and the group's
`min_cursor` / `max_cursor`. The response contains `story_group.story_list`
wrappers. The implementation sends no mutation or viewing-stat requests.
Catalog completeness, item IDs, and authors are checked before accepting data.
Incomplete or changed metadata, stalled responses, and failures stay partial.

Live verification recovered all 40 cataloged IDs in four extra requests:
6 + 6 + 6 + 2 new items, with no foreign authors or unexpected IDs. A story
beyond the initial 20 downloaded successfully. The production listing then
returned 40 unique stories with `complete=true` and an empty `scan_reason`.

The outer author-feed request also needs the app's refresh parameters
`source=9`, `pull_type=1`, and `level=1`. Without them, this account returned
an empty unfinished response without a continuation. With them, it returned
the target author's feed and an explicit completed response.

The Windows app normally starts with only a profile URL. Its web identity
lookup intermittently returned empty responses, so the signed mobile
`/aweme/v1/user/profile/other/` lookup is now preferred with a saved App login.
The returned secondary ID must match the requested profile before its numeric
ID is used. Web lookup remains a fallback. The default identity path was
verified live without supplying a numeric ID manually. Catalog requests retry
one transient failure within the same request budget; login/cancellation
propagate immediately and HTTP 429 is not immediately retried.

Regression coverage includes capped active windows, failed enlargement with
an available archive cursor, last-working request sizes, cancellation, login
rejection, foreign items, stale metadata, saved catalog progress across the
15-request budget, independent archive continuation, and translated summaries.

Local validation used Windows 10 x64 build 19045, CPython 3.11.14, locked release
dependencies, and real FFmpeg tools: 445 tests and 8 subtests passed, including
one independent reproduction outside the repository. Public test fixtures use
synthetic identifiers. Account data, session files, signed URLs, APK contents,
and downloaded private media remain outside the repository.

The portable acceptance run also exposed repeated path migration when a package
was nested under the historical `E:\douyindownload` root. Portable expansion now
applies once, preserves paths already inside the current installation, and
matches complete path components instead of unrelated sibling prefixes.
