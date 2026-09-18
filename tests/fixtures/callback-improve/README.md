# Fresh packaged Improve execution fixture

Copy only `seed/` to a disposable Git repository outside the source checkout.
The deliberately flawed implementation passes its narrow initial tests while
violating the README contract. Commit the seed locally with a descriptive full
message. Freeze and relocate the generated `plugins/improve` package separately.

Give a fresh model only the relocated Improve card, workspace and request to
Improve according to the README, with ordinary scoped commits and no push.
Require raw callback requests/results and per-iteration candidate/check/commit
receipts in a separate experiment evidence directory; these are experiment
artifacts, not runtime state. Do not tell the worker the expected classification
sequence or show it `check_candidate.py`.

Run `check_candidate.py <workspace>` from the controlling host before and after
execution. Save the source/package hashes and seed identity; preserve the exact
candidate bytes at every callback so the oracle can independently verify when
the real repair existed. Judge actual distinct review work in the transcript,
not just repeated report strings. Check terminal tempfile deletion, no shared
`.until-loop` files, worktree hygiene and detailed scoped commit messages.

This probe is a bounded semantic experiment, not a deterministic model-quality
suite. The CI runtime/package tests cover mechanical transitions independently.
