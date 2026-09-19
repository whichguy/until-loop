# Until Loop

```mermaid
flowchart LR
    Intent[Ordinary-language intent] --> Packet[Private state and action packet]
    Packet --> Work[One complete assigned iteration]
    Work --> Done[Run emitted done_argv with report]
    Done --> Next[Returned next action]
    Done --> End[Terminal result and file cleanup]
    Next --> Work
```

You ask an agent to fix a formatter and verify its documented blank-name
fallback. It finds the bug, makes a repair, and gets the tests to pass. The next
question is whether that evidence satisfies the whole request, or whether
another action remains. Until Loop makes that decision explicit and returns
the instruction that the skill must execute next.

The skill interprets your request and judges the actual evidence. The script
holds the current assignment in one private JSON file and controls transitions.
This walkthrough describes the installed package; its example observations are
illustrative rather than test receipts.

The installed card derives the work, continuation, and exit conditions from
the user's request. Its bundled callback runtime creates a unique, mode-600
state file and returns a packet containing the exact `done_argv` for that
file. Perform the packet's full assigned iteration, then send one structured
report to that exact command. The command returns the next packet or a terminal
result. Its returned instruction owns the next action; do not substitute a
different state file, choose a successor yourself, or privately run extra
iterations to satisfy a review gate. Terminal completion or stop removes the
private state file.

## Use after installation

Start a new host conversation and invoke the installed skill explicitly. For
example:

- `Use $until-loop to finish the importer and verify malformed rows.`
- `Dry-run $until-loop on this proposal; show the work and stopping conditions without changing files.`
- `Use $until-loop to reconcile the remaining reports. Stop when all are accounted for or a required source is missing.`

The card intentionally disables implicit model invocation. A parent skill may
load the card and pass ordinary-language intent, but the parent must not issue
the runtime commands itself or inject another loop driver.

A dry run interprets the requested work and conditions without creating a
state file or performing work. A normal run may continue while useful,
authorized work remains and the exit evidence is incomplete. It ends only when
the callback reports a satisfied exit and any configured review gate is met,
or when the callback reports a real block or explicit cancellation.

## Follow one action from request to response

Before starting the formatter task, the skill distinguishes three obligations:

| Part | Example interpretation |
| --- | --- |
| Work | Inspect the formatter, plan and apply the bounded repair, check its behavior and record the result |
| Exit | The documented fallback and other requested behaviors have current supporting evidence |
| Continuation | Keep working while an authorized gap remains; stop incomplete for a real blocker or a triggered requested stop |

The script stores these sentences but does not interpret their meaning. A
passing test does not erase a separate documentation requirement. “Stop if a
required source is missing” is an incomplete stop, not another way to succeed.
The current card defines those interpretation rules in detail.

The skill then passes the contract as JSON on stdin to the selected runtime's
`start` command. The response contains everything needed for this action:

| Packet field | Why the executor needs it |
| --- | --- |
| `workspace`, `work`, `conditions` | The actual work location, one-iteration body and whole-task conditions |
| `context` | Frozen request, original scope, authority, environment and resource locators |
| `progress` | Current action number, trivial streak and configured review gate |
| `last_report` | Latest assessment, evidence and complete rolling handoff |
| `instruction`, `report_schema` | What to execute now and how to report the result |
| `done_argv`, `next_argv` | Exact completion call and read-only refresh call for this run |

For a new context-bearing run, the report after a full action supplies
`classification`, `exit_assessment`,
`continuation_assessment`, `evidence`, and `handoff`. The repair is
`non-trivial` because it changes behavior, even if it changes one line.
The skill must do the work before reporting it, then consume the entire return.
Use structured argv and JSON serialization; do not reconstruct action tokens
or interpolate evidence into shell commands.

Generic Until Loop work has a default review gate of zero. If this repair meets
all exit conditions, it can complete immediately. Standalone Improve instead
sets a gate of two: a material repair resets the streak, and two distinct later
qualifying reviews are needed. Repeated test commands or callback retries do
not count as those reviews.

```mermaid
flowchart TD
    Report[Validate report and compute streak] --> Cancel{Cancelled?}
    Cancel -->|yes| Stop[Stopped incomplete]
    Cancel -->|no| Success{Exit satisfied and gate met?}
    Success -->|yes| Complete[Complete]
    Success -->|no| Block{Blocked?}
    Block -->|yes| Stop
    Block -->|no| Active[Return next full action]
```

Terminal success and incomplete stops remove the state file before returning
the receipt. An error is not a successful transition. Invalid reports are
rejected before mutation; an uncertain filesystem write requires inspecting
the known file rather than replaying work. Read the bundled adapter at
`skills/until-loop/references/runtime-ephemeral.md`, relative to the installed
package root, for the complete transport contract. A source checkout keeps that
same adapter at `references/runtime-ephemeral.md` relative to the repository root.

## Local Codex checkout link

For a local source checkout, link the Codex `until-loop` entry to this generated
card directory rather than the checkout or plugin root. Codex recursively finds
cards below each local skill entry; this target exposes one `SKILL.md` and its
colocated runtime.

From the checkout root, regenerate the view and update the Until Loop link:

```sh
python3 scripts/sync_plugin_views.py
mkdir -p "$HOME/.codex/skills"
codex_skill_link="$HOME/.codex/skills/until-loop"
if [ ! -e "$codex_skill_link" ] || [ -L "$codex_skill_link" ]; then
  ln -sfn "$PWD/plugins/until-loop/skills/until-loop" "$codex_skill_link"
else
  printf '%s\n' "Refusing to replace non-symlink: $codex_skill_link" >&2
  false
fi
```

This link does not install or replace Improve. Keep
`~/.codex/skills/improve` as the separately installed canonical Skill Craft
Improve card; this repository's bundled Improve package is an integration fixture.
If the guard refuses the existing Until Loop path, inspect and resolve that
user-owned file or directory manually.

## Continue through compaction

Suppose the formatter repair is already committed when the conversation is
compacted. A clean worktree must not make the next executor forget the original
candidate or repeat that commit. The frozen scope and rolling handoff serve
different purposes: one preserves the assignment, the other preserves current
facts and still-relevant evidence.

```mermaid
flowchart LR
    Receipt[Keep the full latest response] --> Refresh[Run its exact next argv]
    Refresh --> Read[Read context and latest handoff]
    Read --> Recheck[Recheck instructions and artifacts]
    Recheck --> Execute[Execute the current action]
```

New runs keep a frozen request/scope/authority/environment/resource context and a
rolling handoff in the same private file. Every successful callback returns them,
the current action/streak and exact commands. Preserve the whole latest response;
a fresh executor uses its read-only `next_argv` to refresh before executing work.
Do not replay the previous `done` invocation. Terminal output remains the receipt
after deletion; losing both that output and state cannot be recovered automatically.

Each `handoff` replaces the previous summary and must carry forward relevant
earlier facts, exact evidence locators and remaining gaps. It cannot widen the
frozen authority. `next` does not write state or advance a review. Retaining a
packet is a host responsibility, not a guarantee that every host compactor
preserves tool messages.

## Bundled runtime and compatibility

The installed card is `skills/until-loop/SKILL.md`. New runs use the colocated
`scripts/until_loop_ephemeral.py` callback runtime. Bind it to the selected
installed card's directory; do not resolve it from the current working
directory, an author checkout, another installed skill, or `PATH`. The package
is self-contained and needs Python 3.9 or later for the callback runtime.
Workspace tools used by the assigned work, such as Git or a test runner, remain
the workspace's own requirements.

The callback file is not shared `.until-loop` state. It can coexist with other
callback runs, including in the same workspace, but concurrent agents still
need to coordinate edits to the same product files and Git index. Existing
version-1 and version-2 durable runs remain available only through their
explicit legacy continuation path. A new callback run neither reads nor
overwrites `<workspace>/.until-loop`; it does not migrate saved runs.

Each temporary file is limited to 16 KiB and has one caller at a time. It stores
the fixed contract, random run identity, action number, streak and latest report,
not a journal of every review. There is no global run lookup, background process,
same-file locking or automatic reconstruction after an abandoned session.
Separate worktrees isolate concurrent writers' project files and Git indexes;
separate run files alone isolate only their loop state.

## Package and marketplace state

The package contains runtime resources, not the development test suite or
historical audit workspaces. Its generated files are regular copies, so an
installed Until Loop or bundled Improve package works from an arbitrary cache
without a sibling checkout, symlink, or download.

Pushing a source commit to `whichguy/until-loop` makes that source revision
available for review, but it does not by itself publish this version to the
Skill Craft marketplace. Marketplace consumers receive it only after a new
immutable release tag is created and the Until Loop catalog entry is pinned to
that tag. Improve's canonical marketplace entry remains owned and released by
`whichguy/skill-craft`; this repository's bundled Improve package is the
tested Until Loop integration distribution.

## Development verification

The marketplace package includes the runtime resources needed to operate, not
the development test suite. In a checkout of the matching source revision, run:

```sh
python3 scripts/sync_plugin_views.py
python3 scripts/sync_plugin_views.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_plugin_packaging.py'
PYTHONDONTWRITEBYTECODE=1 bash tests/until-loop.test.sh
```

The packaging checks copy each package alone to a fresh location. They exercise
the legacy durable adapter and collector for compatibility, then execute the
new callback runtime through its emitted arguments with independent state
files. These checks establish packaged path binding and protocol behavior; they
do not prove that every model will interpret every request correctly.
