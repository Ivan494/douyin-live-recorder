# Maintaining the public source and personal installation

Use the GitHub checkout as the only development source. Treat the portable
personal installation as a deployment target with its own data and local
rollback history. Do not develop the two copies independently.

## Normal update

1. Edit a branch in the GitHub checkout.
2. Run `python -m pytest src/tests -q` with the project's dependencies installed.
3. Review and commit the source changes. Keep public defaults empty/sanitized.
4. Preview and then apply the code-only deployment:

   ```powershell
   ./scripts/deploy_personal.ps1 -InstallRoot 'C:\path\to\DouyinLiveRecorder' -WhatIf
   ./scripts/deploy_personal.ps1 -InstallRoot 'C:\path\to\DouyinLiveRecorder'
   ```

5. Restart the installed recorder after its current recordings can safely stop.
   Copying code does not reload an already-running Python application.
6. Push the tested source branch and merge it when ready. Build/tag a public
   release separately; local deployment does not publish anything.

The deployment requires clean Git working trees in both locations and an
existing source-only Git repository in the personal automation directory.
It copies an explicit list of application Python modules and tests, verifies
their hashes, and creates before/after local checkpoint tags. The ignored
`deployed-source.json` records the exact public source commit installed.
Dependencies, launchers, and runtime upgrades require a separate release update.

Never copy the personal installation back wholesale: settings, monitored
profiles, session files, device identifiers, logs, and recordings stay local.
The public repository's shipped settings and empty profiles remain templates.
The personal checkpoint repository must have no GitHub remote.

## Rollback

Stop the personal recorder when safe, then in its `douyindownload/_automation`
directory inspect `git tag --list 'checkpoint-*'`. Restore tracked source from
the chosen checkpoint with `git restore --source <checkpoint> -- .`, inspect
the diff, and commit the rollback. Ignored personal data remains in place.
Restart the app. This is a source rollback, not a dependency rollback.

## On-demand video/story browser

Live recording and the existing API-first media paths remain independent of
an always-running browser. When a browser fallback is needed, the fetch leases
the dedicated Edge CDP endpoint on port 9344 and closes it in `finally` after
the fetch, including failure/cancellation. A later fetch starts it again.
The browser profile and saved login survive ordinary cleanup.

Checks in the recorder process are serialized through browser cleanup so one
check cannot navigate or close another check's browser. An explicitly supplied
available CDP endpoint is borrowed and left open. Do not run a second recorder
process or unrelated automation against the dedicated port. A failure to close
is logged and does not discard successfully fetched media.

This does not remove CDP support: browser fallback still uses CDP briefly.
It removes the need to keep Edge running between checks. No change to a live
recording or to the existing CAPTCHA reset policy is part of this update.
