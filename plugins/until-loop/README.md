# Until Loop

Until Loop turns an ordinary-language task into a durable work record with
continuation and evidence-based exit conditions. It helps an agent choose the
next useful, authorized action and records each accepted transition. It does
not start a background scheduler or prove semantic completion on its own.

## Use after installation

Start a new host conversation and invoke the installed skill explicitly. For
example:

- `Use $until-loop to finish the importer and verify malformed rows.`
- `Dry-run $until-loop on this proposal; show the work and stopping conditions without changing files.`
- `Use $until-loop to reconcile the remaining reports. Stop when all are accounted for or a required source is missing.`

The card intentionally disables implicit model invocation. A parent skill may
load the card and pass ordinary-language intent, but the parent must not issue
the runtime commands itself or inject another loop driver.

## Bundled runtime and dependencies

The installed card is `skills/until-loop/SKILL.md`. Its runtime is the sibling
`scripts/until-loop` adapter, which loads the colocated `until_loop_v2.py` and
`until_loop_packet.py` files and the bundled reference documents. Bind commands
to the selected installed card's directory; do not resolve them from the
current working directory, an author checkout, another installed skill, or
`PATH`.

The package is self-contained and does not download an engine or require a
sibling checkout. The legacy adapter documents Python 3.9+, Bash, and Git on
macOS or Linux. The version-2 adapter is Python-based; the selected workspace's
own task and checks may require additional tools. Host permissions still govern
file edits, commands, network access, and commits.

Until Loop stores its durable run state under `<workspace>/.until-loop`.
Existing version-1 state stays on the legacy adapter; version 2 does not
silently migrate or overwrite it. A dry run validates an interpretation only
and does not initialize or advance a saved run.

## Development verification

The marketplace package includes the runtime resources needed to operate, not
the development test suite or historical audit artifacts. To run repository
checks, use a checkout of the matching release and follow its publishing guide.
