# Audit repairs — 2026-09-15

## Execution contract

Fix all fourteen findings from the installed application's audit, and carry the earlier live-URL/cache fix into this source checkout. Both copies must have matching application code and regression tests. Preserve installed profiles, authentication, settings, and recordings; retain empty/default public configuration in the GitHub checkout. Do not publish personal state.

Validation: turn the audit's defect demonstrations into expected-behavior regression tests, run both complete suites, exercise recording/finalization with FFmpeg, verify source/installation hashes and profile preservation, then load the repaired installed app while preserving active FFmpeg processes. Completion requires every finding mapped to a fix and passing regression evidence.

The initial repair scope was the local GitHub checkout and portable installation. A follow-up request authorized verification and publication of the fixes to GitHub.

## Repairs

The earlier wrong-channel failure is repaired by recognizing Douyin follow/live URLs, prioritizing explicit room URLs, binding cached rooms to their source URL, and clearing stale mappings on edits. The source checkout and portable installation carry the same implementation.

| Finding | Repair | Regression evidence |
| --- | --- | --- |
| F01: failed resume stops monitoring | Keep a numeric recovery timestamp, retain the recovery time limit, and contain failures so other profiles continue. | Failed starts, unchanged recovery age, and continued checks after an error-handler failure. |
| F02: edits/deletion accept obsolete results | Resolve copied profiles; validate ownership under locks before caching or starting; stop an active session when its recording identity changes. Cancel obsolete media work. | In-flight edit/delete, active URL change, and media cancellation on edit/delete/stop. |
| F03: browser messages disappear | Queue decoded frames, retain fragmentation state across reads/timeouts, and answer ping frames. | Coalesced event/reply and fragmented reply across timeout. |
| F04: Stop fails to cancel downloads | Pass a cancellation token through downloads; check requests, waits, browser reads, progress, and chunks; propagate cancellation through retry handlers. | Interrupted download, nested fallback cancellation, and owned-browser cleanup. |
| F05: last deletion does not persist | Explicit deletion can save an empty list; keep the accidental-empty guard and roll back failed changes. | Last-profile deletion and simulated write failure. |
| F06: unsafe redirects | Validate every media redirect before sending it; disable automatic redirect following for byte downloads. | Disallowed redirect never requested; allowed redirect still works. |
| F07: one-off download erases history | Load existing state and merge known media IDs under a lock. | Existing video/story IDs survive a one-off operation. |
| F08: finalizer adopted as live | Reject concat and finalizing output commands; require a recording-session manifest. | Finalization command is not adopted. |
| F09: diagnostic starts recordings | Make `--check` resolve/report only, using a read-only store. | Diagnostic cannot call recording start. |
| F10: story captcha missed | Derive shared captcha state from both videos and stories. | Story-only captcha sets the breaker status. |
| F11: repeated FFmpeg failures spin | Separate recording failures from lookup failures; back off after short exits and reset after sustained recording growth. | Repeated real-shaped process exits increase the delay; a live lookup does not clear it. |
| F12: unreadable config becomes defaults | Treat only a missing file as absent; surface other read errors without saving fallback state. | Permission failure raises and never saves defaults. |
| F13: CLI stderr deadlocks | Write FFmpeg stderr directly to an append log file. | A child writes 1.4 MB of stderr and exits without an unread pipe. |
| F14: adoption loses URL refresh | Save/restore segment timing; reconstruct a deadline for older manifests, including when output is not created yet. | Persisted timing, legacy deadline, and missing output at adoption. |

Related fixes preserve an existing valid media file until its replacement passes validation, use unique temporary downloads, avoid opening a recording log before validating FFmpeg, and locate persistent state beside the executable in frozen builds.

## Validation

- Source suite: **160 passed, 8 subtests passed**, including two public release configuration checks.
- Portable runtime suite: **158 passed, 8 subtests passed**. The two public configuration checks are intentionally separate because an installation contains user settings and profiles.
- Both runs use real FFmpeg/FFprobe for recording tests. The lifecycle fixture serves media over local HTTP, records separate parts, then validates the finalized MKV. Its local fixture allowance is confined to the test; production URL checks remain active.
- Audit regressions are in `src/tests/test_audit_regressions.py`; urgent URL regressions are in `test_live_identity.py` and `test_profile_dialog.py`.
- Release CI now downloads and verifies FFmpeg before running the lifecycle suite and checks public defaults before building. Normal CI also checks public defaults.
- Installation-specific deployment hashes, preserved private-state hashes, process continuity, test transcripts, and live recording evidence are kept in the installation's private audit folder.
- One intermediate installed run failed while Tk read its bundled `text.tcl` during test-window startup. The file exists; 40 standalone Tk create/destroy checks and all three focused GUI tests passed, as did the complete rerun. The cause of this intermittent test startup failure remains unconfirmed; it is not counted as a repaired product defect. The failed transcript is preserved.
- Pre-publication verification exposed delayed cleanup of Tk variables on a worker thread. GUI tests now collect disposed widget cycles on the main thread before later tests create workers. Final verification treats unraisable cleanup warnings as errors. This addresses the observed cleanup warning; its relationship to the earlier Tk initialization error is unconfirmed.

## Coverage and limits

The audit covered the GUI/store, live and media workers, recording/recovery/finalization, browser protocol, downloads/state, URL and tool validation, login integration, CLI, translations, signer integration, launch/stop/build scripts, and tests. All fourteen documented findings have repairs and passing regression coverage. This does not establish that the entire program or its dependencies have no remaining defects.

The verification did not include a dependency vulnerability scan, exhaustive bundled runtime/FFmpeg review, live YouTube recording, new login/logout, cryptographic proof of signer code, destructive disk-exhaustion testing, or remote CI execution/publication. Historical defect demonstrations assert the old broken behavior and should not be used as health tests after these repairs.
