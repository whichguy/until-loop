#!/usr/bin/env python3
"""Build and validate frozen blind-review fixtures for Improve experiments 4 and 5.

The generated corpus deliberately contains no model runner.  A coordinator can
give a fresh reviewer only one opaque case directory, collect its JSON answer
outside the corpus, and use the evaluator-only oracle files afterward.

Examples:
  python3 tests/improve_discovery.py prepare
  python3 tests/improve_discovery.py validate
  python3 tests/improve_discovery.py prepare --root /tmp/discovery-fixtures
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterable, Mapping


FORMAT = "until-loop-improve-discovery-fixtures/v1"
FREEZE_VERSION = "20260914"
HERE = Path(__file__).resolve()
DEFAULT_ROOT = (
    HERE.parent.parent.parent
    / "until-loop-v2-validation"
    / "experiments-20260914"
    / "discovery"
)
DEFAULT_COVERED_CONTROL_ROOT = (
    HERE.parent.parent.parent
    / "until-loop-v2-validation"
    / "experiments-20260914"
    / "discovery-covered-controls"
)
SHARED_REVIEW_POLICY = HERE.parent.parent / "examples" / "improve" / "references" / "review-policy.md"
VISIBLE_TEST_PATTERN = re.compile(r"\bRan ([1-9][0-9]*) tests? in ")


class FixtureError(RuntimeError):
    """Raised when a frozen fixture is malformed, drifted, or not reproducible."""


def normalized(value: str) -> str:
    return textwrap.dedent(value).lstrip("\n")


CSV_REQUIREMENTS = normalized(
    """
    # Required behavior

    `rewrite_csv(text)` accepts CSV text and returns CSV text using the standard
    library. It must preserve every record returned by `csv.reader`, in the same
    order, with the same cell values. A blank input line becomes the empty record
    `[]` when read by `csv.reader`; that record must be written back too. The
    helper may normalize CSV line endings to a single LF character, but it must not silently omit
    a record.
    """
)

CONFIG_REQUIREMENTS = normalized(
    """
    # Required behavior

    `resolve_settings(defaults, file_settings, explicit_settings)` returns a new
    mapping with precedence `defaults < file_settings < explicit_settings`.
    Presence of a key in `explicit_settings` establishes that final override.
    Explicit `False`, `0`, and `""` are all values and must override lower
    precedence values exactly as supplied. Inputs must not be mutated.
    """
)

STATE_REQUIREMENTS = normalized(
    """
    # Required behavior

    `save_state(path, record)` persists exactly the supplied record as JSON.
    Saving a later record replaces the whole previous record: keys absent from
    the new record must not survive. Write the new JSON to a temporary file in
    the target directory, then atomically install it with `os.replace`. The
    function must not mutate the supplied record.
    """
)


CSV_VISIBLE_TEST = normalized(
    """
    import unittest
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    class RecordTests(unittest.TestCase):
        def test_rewrites_nonempty_rows_in_order(self):
            source = "name,amount\\nAda,3\\nMina,4\\n"
            self.assertEqual(records.rewrite_csv(source), source)

        def test_preserves_cell_values(self):
            source = 'name,note\\nAda,"hello, world"\\n'
            self.assertEqual(records.rewrite_csv(source), source)


    if __name__ == "__main__":
        unittest.main()
    """
)

CSV_HIDDEN_ORACLE = normalized(
    """
    # Evaluator-only oracle. Do not give this file to a solver.
    import csv
    import importlib.util
    import sys
    from io import StringIO
    from pathlib import Path


    workspace = Path(sys.argv[1]).resolve()
    source = workspace / "records.py"
    spec = importlib.util.spec_from_file_location("fixture_records", source)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    original = "name,amount\\nAda,3\\n\\nMina,4\\n"
    expected = list(csv.reader(StringIO(original, newline="")))
    actual = list(csv.reader(StringIO(module.rewrite_csv(original), newline="")))
    assert actual == expected, "empty CSV rows must be preserved"
    """
)

CONFIG_VISIBLE_TEST = normalized(
    """
    import unittest
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    class RecordTests(unittest.TestCase):
        def test_file_values_override_defaults(self):
            result = records.resolve_settings({"workers": 1}, {"workers": 3}, {})
            self.assertEqual(result, {"workers": 3})

        def test_truthy_explicit_value_overrides_file_value(self):
            result = records.resolve_settings(
                {"mode": "quiet"}, {"mode": "normal"}, {"mode": "debug"}
            )
            self.assertEqual(result, {"mode": "debug"})


    if __name__ == "__main__":
        unittest.main()
    """
)

CONFIG_HIDDEN_ORACLE = normalized(
    """
    # Evaluator-only oracle. Do not give this file to a solver.
    import importlib.util
    import sys
    from pathlib import Path


    workspace = Path(sys.argv[1]).resolve()
    source = workspace / "records.py"
    spec = importlib.util.spec_from_file_location("fixture_records", source)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    defaults = {"trace": True, "retries": 3, "label": "default"}
    file_values = {"trace": True, "retries": 9, "label": "file"}
    explicit = {"trace": False, "retries": 0, "label": ""}
    actual = module.resolve_settings(defaults, file_values, explicit)
    expected = {"trace": False, "retries": 0, "label": ""}
    assert actual == expected, "explicit falsy settings must override lower-precedence values"
    assert defaults == {"trace": True, "retries": 3, "label": "default"}
    assert file_values == {"trace": True, "retries": 9, "label": "file"}
    assert explicit == {"trace": False, "retries": 0, "label": ""}
    """
)

STATE_VISIBLE_TEST = normalized(
    """
    import tempfile
    import unittest
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    class RecordTests(unittest.TestCase):
        def test_saves_and_loads_one_record(self):
            with tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "state.json"
                record = {"phase": "draft", "count": 1}
                records.save_state(target, record)
                self.assertEqual(records.load_state(target), record)
                self.assertEqual(record, {"phase": "draft", "count": 1})


    if __name__ == "__main__":
        unittest.main()
    """
)

STATE_HIDDEN_ORACLE = normalized(
    """
    # Evaluator-only oracle. Do not give this file to a solver.
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path
    from unittest import mock


    workspace = Path(sys.argv[1]).resolve()
    source = workspace / "records.py"
    spec = importlib.util.spec_from_file_location("fixture_records", source)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "state.json"
        original_replace = module.os.replace
        calls = []

        def observe_replace(source_path, destination_path):
            calls.append((Path(source_path), Path(destination_path)))
            return original_replace(source_path, destination_path)

        with mock.patch.object(module.os, "replace", side_effect=observe_replace):
            module.save_state(target, {"phase": "first", "obsolete": "discard"})
        assert calls and calls[0][0].parent == target.parent, "state writes must use same-directory os.replace"
        assert module.load_state(target) == {"phase": "first", "obsolete": "discard"}

        module.save_state(target, {"phase": "second"})
        assert module.load_state(target) == {"phase": "second"}, (
            "whole-record persistence must discard stale keys"
        )
    """
)


def csv_source(skip_empty: bool) -> str:
    body = "    for row in reader:\n        writer.writerow(row)\n"
    if skip_empty:
        body = "    for row in reader:\n        if not row:\n            continue\n        writer.writerow(row)\n"
    return normalized(
        """
        \"\"\"Small standard-library CSV rewrite helper.\"\"\"
        import csv
        from io import StringIO


        def rewrite_csv(text: str) -> str:
            source = StringIO(text, newline="")
            target = StringIO(newline="")
            reader = csv.reader(source)
            writer = csv.writer(target, lineterminator="\\n")
        """
    ) + body + "    return target.getvalue()\n"


def config_source(skip_falsy: bool) -> str:
    body = "    for name, value in explicit_settings.items():\n        resolved[name] = value\n"
    if skip_falsy:
        body = "    for name, value in explicit_settings.items():\n        if value:\n            resolved[name] = value\n"
    return normalized(
        """
        \"\"\"Small standard-library configuration merge helper.\"\"\"


        def resolve_settings(defaults, file_settings, explicit_settings):
            resolved = dict(defaults)
            resolved.update(file_settings)
        """
    ) + body + "    return resolved\n"


def state_source(merge_previous: bool) -> str:
    payload = "    payload = dict(record)\n"
    if merge_previous:
        payload = "    payload = load_state(target) if target.exists() else {}\n    payload.update(record)\n"
    return normalized(
        """
        \"\"\"Small standard-library JSON state helper.\"\"\"
        import json
        import os
        from pathlib import Path


        def load_state(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))


        def save_state(path, record):
            target = Path(path)
        """
    ) + payload + (
        "    temporary = target.with_name(f\".{target.name}.next\")\n"
        "    temporary.write_text(\n"
        "        json.dumps(payload, sort_keys=True, separators=(\",\", \":\")) + \"\\n\",\n"
        "        encoding=\"utf-8\",\n"
        "    )\n"
        "    os.replace(temporary, target)\n"
    )


def workspace_files(domain: str, flawed: bool) -> dict[str, str]:
    if domain == "csv":
        requirements, source, visible = CSV_REQUIREMENTS, csv_source(flawed), CSV_VISIBLE_TEST
    elif domain == "config":
        requirements, source, visible = CONFIG_REQUIREMENTS, config_source(flawed), CONFIG_VISIBLE_TEST
    elif domain == "state":
        requirements, source, visible = STATE_REQUIREMENTS, state_source(flawed), STATE_VISIBLE_TEST
    else:
        raise FixtureError(f"unknown fixture domain: {domain}")
    return {
        "README.md": normalized(
            """
            # Review fixture workspace

            This is a deliberately small Python standard-library project. Read
            `REQUIREMENTS.md`, the current source, and the visible tests before
            returning the requested review packet.
            """
        ),
        "REQUIREMENTS.md": requirements,
        "records.py": source,
        "tests/test_records.py": visible,
    }


DISCOVERY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "opal-17",
        "domain": "csv",
        "flawed": True,
        "hidden_oracle": CSV_HIDDEN_ORACLE,
        "expected": {
            "classification": "requirement-backed-defect",
            "hidden_should_pass": False,
            "failure_marker": "empty CSV rows must be preserved",
            "findings": [
                {
                    "id": "preserve-empty-reader-records",
                    "materiality": "material",
                    "evidence_paths": ["workspace/records.py", "workspace/REQUIREMENTS.md"],
                    "summary": "The row loop omits the empty reader record despite the preservation requirement.",
                }
            ],
        },
    },
    {
        "id": "lumen-42",
        "domain": "config",
        "flawed": True,
        "hidden_oracle": CONFIG_HIDDEN_ORACLE,
        "expected": {
            "classification": "requirement-backed-defect",
            "hidden_should_pass": False,
            "failure_marker": "explicit falsy settings must override lower-precedence values",
            "findings": [
                {
                    "id": "honor-present-falsy-explicit-values",
                    "materiality": "material",
                    "evidence_paths": ["workspace/records.py", "workspace/REQUIREMENTS.md"],
                    "summary": "Truthiness is used instead of explicit-key presence for final-precedence settings.",
                }
            ],
        },
    },
    {
        "id": "birch-58",
        "domain": "state",
        "flawed": True,
        "hidden_oracle": STATE_HIDDEN_ORACLE,
        "expected": {
            "classification": "requirement-backed-defect",
            "hidden_should_pass": False,
            "failure_marker": "whole-record persistence must discard stale keys",
            "findings": [
                {
                    "id": "replace-whole-state-record",
                    "materiality": "material",
                    "evidence_paths": ["workspace/records.py", "workspace/REQUIREMENTS.md"],
                    "summary": "The writer merges old state with the new record, leaving stale keys behind.",
                }
            ],
        },
    },
    {
        "id": "mosaic-64",
        "domain": "csv",
        "flawed": False,
        "hidden_oracle": CSV_HIDDEN_ORACLE,
        "expected": {
            "classification": "clean-control",
            "hidden_should_pass": True,
            "failure_marker": None,
            "findings": [],
        },
    },
    {
        "id": "quill-71",
        "domain": "config",
        "flawed": False,
        "hidden_oracle": CONFIG_HIDDEN_ORACLE,
        "expected": {
            "classification": "clean-control",
            "hidden_should_pass": True,
            "failure_marker": None,
            "findings": [],
        },
    },
    {
        "id": "sable-23",
        "domain": "state",
        "flawed": False,
        "hidden_oracle": STATE_HIDDEN_ORACLE,
        "expected": {
            "classification": "clean-control",
            "hidden_should_pass": True,
            "failure_marker": None,
            "findings": [],
        },
    },
)

# The public case list is deliberately shuffled so adjacent paths do not reveal
# a pair or a control/defect label.
DISCOVERY_ORDER = ("lumen-42", "mosaic-64", "sable-23", "opal-17", "quill-71", "birch-58")

REVIEWER_VALUE_SPECS: tuple[dict[str, str], ...] = (
    {
        "id": "harbor-96",
        "source_case": "lumen-42",
        "suggestion_id": "memo-44",
        "suggestion": (
            "`workspace/records.py` only writes an explicit setting when its value is truthy. "
            "A present `False`, `0`, or empty string can therefore leave a lower-precedence "
            "value in place. Use key presence for the final override and add coverage for all three values."
        ),
        "expected_kind": "real-bug",
        "expected_decision": "accept",
    },
    {
        "id": "velvet-31",
        "source_case": "sable-23",
        "suggestion_id": "memo-18",
        "suggestion": (
            "Change `save_state` to build a payload with `dict(record)`, write it to a same-directory "
            "temporary file, and install it with `os.replace` so stale keys cannot survive."
        ),
        "expected_kind": "redundant-suggestion",
        "expected_decision": "reject",
    },
    {
        "id": "north-67",
        "source_case": "mosaic-64",
        "suggestion_id": "memo-73",
        "suggestion": (
            "Skip `[]` rows before writing output. Empty records are separators and should not be preserved "
            "in the rewritten CSV."
        ),
        "expected_kind": "confident-wrong-suggestion",
        "expected_decision": "reject",
    },
)


CSV_COVERED_VISIBLE_TEST = normalized(
    """
    import csv
    from io import StringIO
    from pathlib import Path
    import sys
    import unittest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    def reader_rows(text):
        return list(csv.reader(StringIO(text, newline="")))


    class RecordTests(unittest.TestCase):
        def test_preserves_order_and_empty_reader_record(self):
            source = "name,amount\\nAda,3\\n\\nMina,4\\n"
            self.assertEqual(reader_rows(records.rewrite_csv(source)), reader_rows(source))

        def test_preserves_quoted_cell_values(self):
            source = 'name,note\\nAda,"hello, world"\\nMina,"two words"\\n'
            self.assertEqual(reader_rows(records.rewrite_csv(source)), reader_rows(source))


    if __name__ == "__main__":
        unittest.main()
    """
)

CONFIG_COVERED_VISIBLE_TEST = normalized(
    """
    from pathlib import Path
    import sys
    import unittest

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    class RecordTests(unittest.TestCase):
        def test_present_explicit_values_win_even_when_falsy(self):
            defaults = {"trace": True, "retries": 3, "label": "default"}
            file_values = {"trace": True, "retries": 9, "label": "file"}
            explicit = {"trace": False, "retries": 0, "label": ""}

            result = records.resolve_settings(defaults, file_values, explicit)

            self.assertEqual(result, {"trace": False, "retries": 0, "label": ""})
            self.assertEqual(defaults, {"trace": True, "retries": 3, "label": "default"})
            self.assertEqual(file_values, {"trace": True, "retries": 9, "label": "file"})
            self.assertEqual(explicit, {"trace": False, "retries": 0, "label": ""})

        def test_file_values_win_when_no_explicit_value_is_present(self):
            result = records.resolve_settings({"workers": 1}, {"workers": 3}, {})
            self.assertEqual(result, {"workers": 3})


    if __name__ == "__main__":
        unittest.main()
    """
)

STATE_COVERED_VISIBLE_TEST = normalized(
    """
    from pathlib import Path
    import sys
    import tempfile
    import unittest
    from unittest import mock

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import records


    class RecordTests(unittest.TestCase):
        def test_replaces_the_whole_record_with_a_same_directory_install(self):
            with tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "state.json"
                original_replace = records.os.replace
                calls = []

                def observe_replace(source_path, destination_path):
                    calls.append((Path(source_path), Path(destination_path)))
                    return original_replace(source_path, destination_path)

                first = {"phase": "first", "obsolete": "discard"}
                with mock.patch.object(records.os, "replace", side_effect=observe_replace):
                    records.save_state(target, first)
                self.assertTrue(calls)
                self.assertEqual(calls[0][0].parent, target.parent)
                self.assertEqual(calls[0][1], target)
                self.assertEqual(records.load_state(target), first)
                self.assertEqual(first, {"phase": "first", "obsolete": "discard"})

                second = {"phase": "second"}
                records.save_state(target, second)
                self.assertEqual(records.load_state(target), second)
                self.assertEqual(second, {"phase": "second"})
                self.assertFalse((target.parent / ".state.json.next").exists())


    if __name__ == "__main__":
        unittest.main()
    """
)

COVERED_CONTROL_SPECS: tuple[dict[str, str], ...] = (
    {"id": "glacier-49", "domain": "csv", "visible_test": CSV_COVERED_VISIBLE_TEST},
    {"id": "spruce-82", "domain": "config", "visible_test": CONFIG_COVERED_VISIBLE_TEST},
    {"id": "ember-35", "domain": "state", "visible_test": STATE_COVERED_VISIBLE_TEST},
)
COVERED_CONTROL_ORDER = tuple(spec["id"] for spec in COVERED_CONTROL_SPECS)


def dedented_schema(case_id: str, *, triage: bool = False) -> str:
    schema: dict[str, Any] = {
        "case_id": case_id,
        "findings": [
            {
                "summary": "concrete observed issue or an empty list",
                "evidence": [
                    {
                        "path": "workspace/records.py",
                        "lines": "1-2",
                        "detail": "what the file shows",
                    }
                ],
                "materiality": "material|trivial|unknown",
                "uncertainty": "known limitation or null",
            }
        ],
        "plan": [
            {
                "step": "bounded next step",
                "paths": ["workspace/records.py"],
                "reason": "requirement basis",
            }
        ],
        "tests_proposed": [
            {
                "description": "behavior to cover",
                "paths": ["workspace/tests/test_records.py"],
                "covers": "requirement clause",
            }
        ],
        "materiality": "material|trivial|none|unknown",
        "uncertainty": ["facts that could not be established from the supplied files"],
    }
    if triage:
        schema = {
            "case_id": case_id,
            "suggestion_triage": {
                "suggestion_id": "<packet suggestion id>",
                "origin": "simulated",
                "decision": "accept|reject|needs_evidence",
                "reason": "requirement and current-file basis",
            },
            **{key: value for key, value in schema.items() if key != "case_id"},
        }
    return json.dumps(schema, indent=2)


def review_packet(case_id: str, requirements: str, *, triage: Mapping[str, str] | None = None) -> str:
    parts = [
        f"# Read-only review packet: {case_id}\n\n",
        "Review this small current workspace and return a plan only. Do not edit\n",
        "files, commit, create runtime state, initialize a loop, or execute a full\n",
        "Improve/until-loop workflow. You may inspect the supplied source and\n",
        "visible tests. Return one JSON object only.\n\n",
        "Evaluator-only artifacts are deliberately outside this supplied case.\n",
        "Do not inspect paths outside the case directory. This is an instruction\n",
        "boundary, not an operating-system access control boundary.\n\n",
        "## Exact requirements\n\n",
        requirements.rstrip(),
        "\n\n## Current files to review\n\n",
        "Source and requirements:\n",
        "- `workspace/records.py`\n",
        "- `workspace/REQUIREMENTS.md`\n\n",
        "Visible tests:\n",
        "- `workspace/tests/test_records.py`\n",
    ]
    if triage is not None:
        parts.extend(
            [
                "\n## Simulated suggestion to triage\n\n",
                f"Suggestion ID: `{triage['suggestion_id']}`\n\n",
                "Origin: `simulated` (this is experiment conditioning, not a genuine\n",
                "reviewer result).\n\n",
                f"> {triage['suggestion']}\n\n",
                "Evaluate this suggestion against the supplied files and requirements.\n",
                "Accepting a suggestion requires a current requirement-and-evidence\n",
                "basis; reject it when it is redundant or contradicted by the current\n",
                "requirement. Keep `suggestion_triage.origin` exactly `simulated`.\n",
            ]
        )
    parts.extend(
        [
            "\n## Required JSON shape\n\n```json\n",
            dedented_schema(case_id, triage=triage is not None),
            "\n```\n\n",
            "Use an empty `findings` list when the supplied requirements and current\n",
            "files establish no issue. Do not claim unobserved test results as facts.\n",
        ]
    )
    return "".join(parts)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureError(f"cannot read JSON fixture {path}: {error}") from error


def require_new(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise FixtureError(f"{label} must be a new path: {path}")


def spec_by_id(case_id: str) -> dict[str, Any]:
    for spec in DISCOVERY_SPECS:
        if spec["id"] == case_id:
            return spec
    raise FixtureError(f"unknown discovery fixture ID: {case_id}")


def requirements_for(spec: Mapping[str, Any]) -> str:
    return workspace_files(str(spec["domain"]), bool(spec["flawed"]))["REQUIREMENTS.md"]


def file_records(root: Path, scopes: Iterable[Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for scope in scopes:
        if scope.is_symlink() or not scope.exists():
            raise FixtureError(f"manifest scope is missing or unsafe: {scope}")
        if scope.is_file():
            candidates = [scope]
        else:
            candidates = sorted(path for path in scope.rglob("*") if path.is_file() or path.is_symlink())
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                raise FixtureError(f"fixture contains an unsafe non-file entry: {path}")
            relative = str(path.relative_to(root))
            raw = path.read_bytes()
            records[relative] = {"bytes": len(raw), "sha256": sha256_bytes(raw)}
    return dict(sorted(records.items()))


def manifest_payload(root: Path, scopes: Iterable[Path], kind: str) -> dict[str, Any]:
    return {"format": FORMAT, "kind": kind, "files": file_records(root, scopes)}


def source_files(spec: Mapping[str, Any]) -> dict[str, str]:
    return workspace_files(str(spec["domain"]), bool(spec["flawed"]))


def write_workspace(destination: Path, spec: Mapping[str, Any]) -> None:
    for relative, content in source_files(spec).items():
        write_text(destination / "workspace" / relative, content)


def verify_expected_spec_shape() -> None:
    ids = [str(spec["id"]) for spec in DISCOVERY_SPECS]
    if len(ids) != 6 or len(ids) != len(set(ids)) or set(ids) != set(DISCOVERY_ORDER):
        raise FixtureError("discovery specs must have six uniquely ordered opaque IDs")
    domains = [str(spec["domain"]) for spec in DISCOVERY_SPECS]
    if sorted(domains) != ["config", "config", "csv", "csv", "state", "state"]:
        raise FixtureError("discovery specs must contain the three matched domains")
    for domain in {"csv", "config", "state"}:
        variants = [bool(spec["flawed"]) for spec in DISCOVERY_SPECS if spec["domain"] == domain]
        if sorted(variants) != [False, True]:
            raise FixtureError(f"{domain} specs must have one clean and one flawed fixture")
    reviewer_ids = [spec["id"] for spec in REVIEWER_VALUE_SPECS]
    if len(reviewer_ids) != 3 or len(reviewer_ids) != len(set(reviewer_ids)):
        raise FixtureError("reviewer-value specs must have three unique opaque IDs")
    if {spec["expected_kind"] for spec in REVIEWER_VALUE_SPECS} != {
        "real-bug", "redundant-suggestion", "confident-wrong-suggestion",
    }:
        raise FixtureError("reviewer-value specs must cover all three challenge arms")


def expected_findings_payload() -> dict[str, Any]:
    return {
        "format": FORMAT,
        "visibility": "evaluator-only",
        "discovery": {
            str(spec["id"]): spec["expected"]
            for spec in sorted(DISCOVERY_SPECS, key=lambda item: str(item["id"]))
        },
        "pair_map": {
            "csv": ["opal-17", "mosaic-64"],
            "config": ["lumen-42", "quill-71"],
            "state": ["birch-58", "sable-23"],
        },
    }


def reviewer_expectations_payload() -> dict[str, Any]:
    return {
        "format": FORMAT,
        "visibility": "evaluator-only",
        "reviewer_value": {
            spec["id"]: {
                "source_discovery_case": spec["source_case"],
                "suggestion_id": spec["suggestion_id"],
                "suggestion_origin": "simulated",
                "challenge_kind": spec["expected_kind"],
                "expected_decision": spec["expected_decision"],
            }
            for spec in REVIEWER_VALUE_SPECS
        },
    }


def root_readme() -> str:
    return normalized(
        """
        # Frozen blinded discovery corpus

        This corpus prepares family 4 blind defect-discovery fixtures and family
        5 reviewer-value fixtures. It does not invoke a model. Give a solver one
        opaque case directory at a time and save its answer outside this frozen
        directory. Do not share `evaluator/` with a solver.

        The separation is procedural only: the prompt repeats that it is an
        instruction boundary, not an operating-system sandbox. The coordinator
        is responsible for presenting only the intended case files.

        Validate an unchanged corpus with:

        ```text
        python3 /Users/dadleet/src/until-loop-v2/tests/improve_discovery.py validate \\
          --root /Users/dadleet/src/until-loop-v2-validation/experiments-20260914/discovery
        ```

        `manifests/` holds SHA-256 freeze records. Any output written inside this
        directory intentionally invalidates the freeze; place model transcripts
        and grades beside the corpus instead.
        """
    )


def solver_readme() -> str:
    return normalized(
        """
        # Solver-visible discovery cases

        Each opaque directory is an independent, read-only review-and-plan task.
        Give a solver only one directory and its packet. Do not reveal the
        sibling directories, evaluator files, pair mapping, or result labels.
        """
    )


def reviewer_readme() -> str:
    return normalized(
        """
        # Reviewer-value challenge cases

        For each opaque case, run `SELF_REVIEW_PACKET.md` first as an unprimed
        baseline. Only afterward provide `TRIAGE_PACKET.md`. The embedded
        suggestion in this frozen corpus is explicitly simulated, not a genuine
        reviewer result. Store answers outside the frozen corpus.

        To compare a genuine reviewer later, retain the same self-review packet,
        replace only the quoted suggestion with the genuine output, and label its
        origin `genuine_reviewer` in the coordinator's wrapper rather than
        rewriting this frozen packet.
        """
    )


def evaluator_readme() -> str:
    return normalized(
        """
        # Evaluator-only artifacts

        This directory contains hidden behavior oracles, expected findings, pair
        relationships, and reviewer-value triage expectations. It is not solver
        input. The filesystem does not enforce that distinction; coordinators
        must preserve it when handing a case to a fresh reviewer.
        """
    )


def prepare(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Create one immutable corpus at a previously unused destination."""
    verify_expected_spec_shape()
    root = root.resolve()
    require_new(root, "fixture root")
    write_text(root / "README.md", root_readme())
    write_text(root / "solver" / "README.md", solver_readme())

    for case_id in DISCOVERY_ORDER:
        spec = spec_by_id(case_id)
        case_root = root / "solver" / "cases" / case_id
        write_workspace(case_root, spec)
        write_text(case_root / "REVIEW_PACKET.md", review_packet(case_id, requirements_for(spec)))
    write_json(
        root / "solver" / "case-index.json",
        {
            "format": FORMAT,
            "case_ids": list(DISCOVERY_ORDER),
            "packet": "REVIEW_PACKET.md",
            "workspace": "workspace",
            "output": "one JSON object matching the packet schema",
        },
    )

    write_text(root / "reviewer-value" / "README.md", reviewer_readme())
    for challenge in REVIEWER_VALUE_SPECS:
        source_case = spec_by_id(challenge["source_case"])
        case_root = root / "reviewer-value" / "cases" / challenge["id"]
        write_workspace(case_root, source_case)
        requirements = requirements_for(source_case)
        write_text(case_root / "SELF_REVIEW_PACKET.md", review_packet(challenge["id"], requirements))
        write_text(
            case_root / "TRIAGE_PACKET.md",
            review_packet(challenge["id"], requirements, triage=challenge),
        )
    write_json(
        root / "reviewer-value" / "case-index.json",
        {
            "format": FORMAT,
            "case_ids": [spec["id"] for spec in REVIEWER_VALUE_SPECS],
            "baseline_packet": "SELF_REVIEW_PACKET.md",
            "triage_packet": "TRIAGE_PACKET.md",
            "suggestion_origin": "simulated",
        },
    )

    write_text(root / "evaluator" / "README.md", evaluator_readme())
    for spec in DISCOVERY_SPECS:
        write_text(root / "evaluator" / "hidden-oracles" / f"{spec['id']}.py", spec["hidden_oracle"])
    write_json(root / "evaluator" / "expected-findings.json", expected_findings_payload())
    write_json(root / "evaluator" / "reviewer-value-expectations.json", reviewer_expectations_payload())

    public_scopes = (root / "README.md", root / "solver", root / "reviewer-value")
    evaluator_scopes = (root / "evaluator",)
    public_manifest = manifest_payload(root, public_scopes, "solver-visible-freeze")
    evaluator_manifest = manifest_payload(root, evaluator_scopes, "evaluator-only-freeze")
    write_json(root / "manifests" / "public-sha256.json", public_manifest)
    write_json(root / "manifests" / "evaluator-sha256.json", evaluator_manifest)
    write_json(
        root / "manifests" / "case-freeze.json",
        {
            "format": FORMAT,
            "freeze_version": FREEZE_VERSION,
            "discovery_case_ids": list(DISCOVERY_ORDER),
            "reviewer_value_case_ids": [spec["id"] for spec in REVIEWER_VALUE_SPECS],
            "public_manifest_sha256": sha256_bytes((root / "manifests" / "public-sha256.json").read_bytes()),
            "evaluator_manifest_sha256": sha256_bytes((root / "manifests" / "evaluator-sha256.json").read_bytes()),
        },
    )
    report = validate(root)
    return {
        "format": FORMAT,
        "prepared": True,
        "root": str(root),
        "discovery_cases": len(DISCOVERY_SPECS),
        "reviewer_value_cases": len(REVIEWER_VALUE_SPECS),
        "validation": report,
    }


def assert_canonical_json(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    value = read_json(path)
    expected = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if raw != expected:
        raise FixtureError(f"JSON fixture is not canonical: {path}")


def assert_manifest(root: Path, name: str, scopes: Iterable[Path], kind: str) -> None:
    path = root / "manifests" / name
    actual = read_json(path)
    expected = manifest_payload(root, scopes, kind)
    if actual != expected:
        raise FixtureError(f"{name} does not match the current frozen files")


def run_process(command: list[str], cwd: Path) -> dict[str, Any]:
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": None,
            "timed_out": True,
            "stdout": (error.stdout or b"").decode("utf-8", "replace"),
            "stderr": (error.stderr or b"").decode("utf-8", "replace"),
        }
    return {
        "command": command,
        "cwd": str(cwd),
        "returncode": result.returncode,
        "stdout": result.stdout.decode("utf-8", "replace"),
        "stderr": result.stderr.decode("utf-8", "replace"),
    }


def run_visible_tests(workspace: Path) -> dict[str, Any]:
    return run_process([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], workspace)


def run_hidden_oracle(root: Path, case_id: str, workspace: Path) -> dict[str, Any]:
    oracle = root / "evaluator" / "hidden-oracles" / f"{case_id}.py"
    return run_process([sys.executable, str(oracle), str(workspace)], workspace)


def assert_case_contents(root: Path, spec: Mapping[str, Any]) -> None:
    case_root = root / "solver" / "cases" / str(spec["id"])
    expected_files = source_files(spec)
    for relative, expected in expected_files.items():
        path = case_root / "workspace" / relative
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise FixtureError(f"discovery source drifted: {path}")
    packet = case_root / "REVIEW_PACKET.md"
    required_packet_bits = (
        f"# Read-only review packet: {spec['id']}",
        "instruction boundary, not an operating-system access control boundary",
        "Do not edit",
        "create runtime state",
        "workspace/records.py",
        "workspace/tests/test_records.py",
        "\"findings\"",
        "\"tests_proposed\"",
        "\"materiality\"",
        "\"uncertainty\"",
    )
    packet_text = packet.read_text(encoding="utf-8") if packet.is_file() else ""
    compact_packet = " ".join(packet_text.split())
    if any(" ".join(bit.split()) not in compact_packet for bit in required_packet_bits):
        raise FixtureError(f"discovery packet is incomplete: {packet}")
    if " ".join(requirements_for(spec).split()) not in compact_packet:
        raise FixtureError(f"discovery packet does not carry exact requirements: {packet}")


def exercise_discovery_case(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(spec["id"])
    workspace = root / "solver" / "cases" / case_id / "workspace"
    visible = run_visible_tests(workspace)
    hidden = run_hidden_oracle(root, case_id, workspace)
    visible_summary = f"{visible['stdout']}\n{visible['stderr']}"
    if visible["returncode"] != 0 or not VISIBLE_TEST_PATTERN.search(visible_summary):
        raise FixtureError(f"visible tests did not pass meaningfully for {case_id}: {visible_summary.strip()}")
    expected = spec["expected"]
    should_pass = bool(expected["hidden_should_pass"])
    if should_pass and hidden["returncode"] != 0:
        raise FixtureError(f"hidden oracle unexpectedly failed for clean fixture {case_id}: {hidden['stderr'].strip()}")
    if not should_pass:
        marker = expected["failure_marker"]
        hidden_summary = f"{hidden['stdout']}\n{hidden['stderr']}"
        if hidden["returncode"] == 0 or not isinstance(marker, str) or marker not in hidden_summary:
            raise FixtureError(f"hidden oracle did not expose its expected defect for {case_id}")
    return {
        "case_id": case_id,
        "visible": {"returncode": visible["returncode"], "tests_ran": int(VISIBLE_TEST_PATTERN.search(visible_summary).group(1))},
        "hidden": {"returncode": hidden["returncode"], "expected_pass": should_pass},
    }


def assert_reviewer_value_contents(root: Path, challenge: Mapping[str, str]) -> None:
    case_root = root / "reviewer-value" / "cases" / challenge["id"]
    source_case = spec_by_id(challenge["source_case"])
    expected_files = source_files(source_case)
    for relative, expected in expected_files.items():
        path = case_root / "workspace" / relative
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise FixtureError(f"reviewer-value source drifted: {path}")
    baseline = (case_root / "SELF_REVIEW_PACKET.md").read_text(encoding="utf-8")
    triage = (case_root / "TRIAGE_PACKET.md").read_text(encoding="utf-8")
    if challenge["suggestion"] in baseline or "Simulated suggestion to triage" in baseline:
        raise FixtureError(f"reviewer-value baseline is primed: {challenge['id']}")
    required_triage_bits = (
        challenge["suggestion"],
        challenge["suggestion_id"],
        "Origin: `simulated`",
        "not a genuine\nreviewer result",
        "\"suggestion_triage\"",
        "\"findings\"",
        "\"tests_proposed\"",
    )
    compact_triage = " ".join(triage.split())
    if any(" ".join(bit.split()) not in compact_triage for bit in required_triage_bits):
        raise FixtureError(f"reviewer-value triage packet is incomplete: {challenge['id']}")


def validate(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Check manifests, prompt boundaries, and all visible/hidden behavior gates."""
    verify_expected_spec_shape()
    root = root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise FixtureError(f"fixture root is missing or unsafe: {root}")
    public_scopes = (root / "README.md", root / "solver", root / "reviewer-value")
    evaluator_scopes = (root / "evaluator",)
    assert_manifest(root, "public-sha256.json", public_scopes, "solver-visible-freeze")
    assert_manifest(root, "evaluator-sha256.json", evaluator_scopes, "evaluator-only-freeze")
    freeze = read_json(root / "manifests" / "case-freeze.json")
    if not isinstance(freeze, dict) or freeze.get("format") != FORMAT or freeze.get("freeze_version") != FREEZE_VERSION:
        raise FixtureError("case freeze metadata has an unsupported format")
    if freeze.get("discovery_case_ids") != list(DISCOVERY_ORDER):
        raise FixtureError("case freeze metadata has an unexpected discovery order")
    if freeze.get("reviewer_value_case_ids") != [spec["id"] for spec in REVIEWER_VALUE_SPECS]:
        raise FixtureError("case freeze metadata has unexpected reviewer-value IDs")
    for filename in ("public-sha256.json", "evaluator-sha256.json"):
        expected_hash = freeze.get(f"{filename.removesuffix('.json').replace('-', '_')}_sha256")
        # The field spellings intentionally use public_manifest/evaluator_manifest.
        if filename == "public-sha256.json":
            expected_hash = freeze.get("public_manifest_sha256")
        else:
            expected_hash = freeze.get("evaluator_manifest_sha256")
        if expected_hash != sha256_bytes((root / "manifests" / filename).read_bytes()):
            raise FixtureError(f"case freeze digest changed for {filename}")
    for path in sorted(root.rglob("*.json")):
        assert_canonical_json(path)

    expected_findings = read_json(root / "evaluator" / "expected-findings.json")
    reviewer_expectations = read_json(root / "evaluator" / "reviewer-value-expectations.json")
    if expected_findings != expected_findings_payload():
        raise FixtureError("evaluator expected findings do not match the frozen fixture definitions")
    if reviewer_expectations != reviewer_expectations_payload():
        raise FixtureError("reviewer-value expectations do not match the frozen fixture definitions")

    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for scope in public_scopes
        for path in ([scope] if scope.is_file() else sorted(scope.rglob("*")))
        if path.is_file()
    )
    for spec in DISCOVERY_SPECS:
        for finding in spec["expected"]["findings"]:
            if finding["id"] in public_text:
                raise FixtureError("solver-visible files leak evaluator finding labels")
    if "clean-control" in public_text or "requirement-backed-defect" in public_text:
        raise FixtureError("solver-visible files leak discovery classifications")

    discovery_reports = []
    for case_id in DISCOVERY_ORDER:
        spec = spec_by_id(case_id)
        assert_case_contents(root, spec)
        discovery_reports.append(exercise_discovery_case(root, spec))
    for challenge in REVIEWER_VALUE_SPECS:
        assert_reviewer_value_contents(root, challenge)
    return {
        "format": FORMAT,
        "root": str(root),
        "frozen": True,
        "discovery_cases": discovery_reports,
        "reviewer_value_cases": [spec["id"] for spec in REVIEWER_VALUE_SPECS],
    }


def covered_spec_by_id(case_id: str) -> dict[str, str]:
    for spec in COVERED_CONTROL_SPECS:
        if spec["id"] == case_id:
            return spec
    raise FixtureError(f"unknown covered-control fixture ID: {case_id}")


def covered_workspace_files(spec: Mapping[str, str]) -> dict[str, str]:
    files = workspace_files(spec["domain"], False)
    files["tests/test_records.py"] = spec["visible_test"]
    return files


def read_shared_policy() -> bytes:
    if SHARED_REVIEW_POLICY.is_symlink() or not SHARED_REVIEW_POLICY.is_file():
        raise FixtureError(f"shared review policy is not a regular file: {SHARED_REVIEW_POLICY}")
    return SHARED_REVIEW_POLICY.read_bytes()


def covered_owner_binding(case_id: str) -> str:
    return normalized(
        f"""
        # Covered-control owner binding: {case_id}

        Read `policy/REVIEW_POLICY.md` in full before this binding. This owner
        assigns one read-only review-and-plan phase only.

        ## History and candidate scope

        History window: none. This is a non-Git static fixture, so no commit
        history exists or should be inferred. Review only these current files:

        - `workspace/REQUIREMENTS.md`
        - `workspace/records.py`
        - `workspace/tests/test_records.py`

        Adjacent files and paths outside this supplied case are out of scope.

        ## Classification and planning

        Material findings include violated required behavior and missing required
        regression coverage. Trivial findings are nonsemantic polish only.
        Investigate insufficient evidence before classifying it as uncertain.
        Current adequate tests are retained. An empty plan is valid when the
        supplied requirements, source, and tests establish no worthwhile work.

        ## Evidence, phase, and callback

        Cite current in-scope file paths and line evidence in the JSON callback
        to the coordinator. This phase authorizes no edits, commits, full
        convergence, standalone adapter, or later execution phase. Do not start
        a loop or create runtime state. The coordinator alone receives the JSON
        callback and decides whether any later phase is authorized.
        """
    )


def covered_schema(case_id: str) -> str:
    original = json.loads(dedented_schema(case_id))
    schema = {
        "case_id": case_id,
        "callback": {
            "recipient": "coordinator",
            "phase": "read_only_review_plan",
        },
        **{key: value for key, value in original.items() if key != "case_id"},
    }
    return json.dumps(schema, indent=2)


def covered_review_packet(case_id: str, requirements: str) -> str:
    return "".join(
        [
            f"# Read-only covered-control review packet: {case_id}\n\n",
            "Before reviewing, read `policy/REVIEW_POLICY.md` in full and then\n",
            "`OWNER_BINDING.md`. They define this owner-managed phase.\n\n",
            "Review the supplied current workspace and return a plan only. Do not\n",
            "edit files, commit, create runtime state, initialize a loop, invoke a\n",
            "standalone adapter, or perform full convergence. Return one JSON object\n",
            "only, using the required callback shape.\n\n",
            "Do not inspect paths outside this supplied case directory. This is an\n",
            "instruction boundary, not an operating-system access control boundary.\n\n",
            "## Exact requirements\n\n",
            requirements.rstrip(),
            "\n\n## Current review scope\n\n",
            "- `workspace/REQUIREMENTS.md`\n",
            "- `workspace/records.py`\n",
            "- `workspace/tests/test_records.py`\n",
            "- `policy/REVIEW_POLICY.md`\n",
            "- `OWNER_BINDING.md`\n\n",
            "## Required JSON callback\n\n```json\n",
            covered_schema(case_id),
            "\n```\n\n",
            "Use an empty `findings` list and empty `plan` list when the current\n",
            "requirements and adequate tests establish no worthwhile work. Do not\n",
            "claim unobserved test results as facts.\n",
        ]
    )


def covered_root_readme() -> str:
    return normalized(
        """
        # Frozen covered-control review fixtures

        These three independent cases are read-only owner-managed review-and-plan
        inputs. Give a fresh reviewer only one opaque directory under
        `solver/cases/`. Each case includes the complete frozen review policy and
        an explicit owner binding, so no external policy path is required.

        Keep reviewer JSON responses outside this frozen directory. The fixtures
        contain no hidden expected-answer or grading material.
        """
    )


def covered_solver_readme() -> str:
    return normalized(
        """
        # Solver-visible covered controls

        Each opaque case is independent. The coordinator supplies one case at a
        time and receives its JSON callback. Do not disclose sibling cases as
        context for the review.
        """
    )


def verify_covered_spec_shape() -> None:
    if len(COVERED_CONTROL_SPECS) != 3:
        raise FixtureError("covered controls must have exactly three cases")
    if len(COVERED_CONTROL_ORDER) != len(set(COVERED_CONTROL_ORDER)):
        raise FixtureError("covered controls must have unique opaque IDs")
    if {spec["domain"] for spec in COVERED_CONTROL_SPECS} != {"csv", "config", "state"}:
        raise FixtureError("covered controls must include CSV, config, and state")
    for spec in COVERED_CONTROL_SPECS:
        if spec["visible_test"] == workspace_files(spec["domain"], False)["tests/test_records.py"]:
            raise FixtureError("covered control cannot reuse an under-covered visible suite")


def write_covered_workspace(destination: Path, spec: Mapping[str, str]) -> None:
    for relative, content in covered_workspace_files(spec).items():
        write_text(destination / "workspace" / relative, content)


def run_builtin_oracle(spec: Mapping[str, str], workspace: Path) -> dict[str, Any]:
    oracle_by_domain = {
        "csv": CSV_HIDDEN_ORACLE,
        "config": CONFIG_HIDDEN_ORACLE,
        "state": STATE_HIDDEN_ORACLE,
    }
    try:
        oracle = oracle_by_domain[spec["domain"]]
    except KeyError as error:
        raise FixtureError(f"unknown covered-control oracle domain: {spec['domain']}") from error
    return run_process([sys.executable, "-c", oracle, str(workspace)], workspace)


def prepare_covered_controls(root: Path = DEFAULT_COVERED_CONTROL_ROOT) -> dict[str, Any]:
    """Create frozen clean controls with visible coverage for every requirement."""
    verify_covered_spec_shape()
    root = root.resolve()
    require_new(root, "covered-control fixture root")
    policy = read_shared_policy()
    write_text(root / "README.md", covered_root_readme())
    write_text(root / "solver" / "README.md", covered_solver_readme())
    for case_id in COVERED_CONTROL_ORDER:
        spec = covered_spec_by_id(case_id)
        case_root = root / "solver" / "cases" / case_id
        write_covered_workspace(case_root, spec)
        write_text(case_root / "policy" / "REVIEW_POLICY.md", policy.decode("utf-8"))
        write_text(case_root / "OWNER_BINDING.md", covered_owner_binding(case_id))
        requirements = covered_workspace_files(spec)["REQUIREMENTS.md"]
        write_text(case_root / "REVIEW_PACKET.md", covered_review_packet(case_id, requirements))
    write_json(
        root / "solver" / "case-index.json",
        {
            "format": FORMAT,
            "case_ids": list(COVERED_CONTROL_ORDER),
            "packet": "REVIEW_PACKET.md",
            "owner_binding": "OWNER_BINDING.md",
            "policy": "policy/REVIEW_POLICY.md",
            "workspace": "workspace",
            "output": "one JSON callback to the coordinator",
        },
    )
    public_scopes = (root / "README.md", root / "solver")
    public_manifest = manifest_payload(root, public_scopes, "covered-control-freeze")
    write_json(root / "manifests" / "public-sha256.json", public_manifest)
    write_json(
        root / "manifests" / "case-freeze.json",
        {
            "format": FORMAT,
            "freeze_version": FREEZE_VERSION,
            "covered_control_case_ids": list(COVERED_CONTROL_ORDER),
            "policy_sha256": sha256_bytes(policy),
            "public_manifest_sha256": sha256_bytes((root / "manifests" / "public-sha256.json").read_bytes()),
        },
    )
    report = validate_covered_controls(root)
    return {
        "format": FORMAT,
        "prepared": True,
        "root": str(root),
        "covered_control_cases": len(COVERED_CONTROL_SPECS),
        "validation": report,
    }


def assert_covered_control_contents(root: Path, spec: Mapping[str, str], policy: bytes) -> None:
    case_root = root / "solver" / "cases" / spec["id"]
    for relative, expected in covered_workspace_files(spec).items():
        path = case_root / "workspace" / relative
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise FixtureError(f"covered-control source drifted: {path}")
    policy_path = case_root / "policy" / "REVIEW_POLICY.md"
    if policy_path.is_symlink() or not policy_path.is_file() or policy_path.read_bytes() != policy:
        raise FixtureError(f"covered-control policy snapshot drifted: {policy_path}")
    binding = (case_root / "OWNER_BINDING.md").read_text(encoding="utf-8")
    binding_bits = (
        "History window: none.",
        "non-Git static fixture",
        "workspace/REQUIREMENTS.md",
        "workspace/records.py",
        "workspace/tests/test_records.py",
        "violated required behavior and missing required\nregression coverage",
        "Trivial findings are nonsemantic polish only.",
        "Current adequate tests are retained.",
        "An empty plan is valid",
        "no edits, commits, full\nconvergence, standalone adapter",
        "JSON\ncallback",
    )
    compact_binding = " ".join(binding.split())
    if any(" ".join(bit.split()) not in compact_binding for bit in binding_bits):
        raise FixtureError(f"covered-control owner binding is incomplete: {case_root}")
    packet = (case_root / "REVIEW_PACKET.md").read_text(encoding="utf-8")
    packet_bits = (
        "policy/REVIEW_POLICY.md",
        "OWNER_BINDING.md",
        "Do not\nedit files, commit, create runtime state, initialize a loop, invoke a\nstandalone adapter, or perform full convergence.",
        "instruction boundary, not an operating-system access control boundary",
        "\"callback\"",
        "\"findings\"",
        "\"plan\"",
        "\"tests_proposed\"",
        "\"materiality\"",
        "\"uncertainty\"",
    )
    compact_packet = " ".join(packet.split())
    if any(" ".join(bit.split()) not in compact_packet for bit in packet_bits):
        raise FixtureError(f"covered-control packet is incomplete: {case_root}")
    if " ".join(covered_workspace_files(spec)["REQUIREMENTS.md"].split()) not in compact_packet:
        raise FixtureError(f"covered-control packet lacks exact requirements: {case_root}")


def exercise_covered_control(root: Path, spec: Mapping[str, str]) -> dict[str, Any]:
    workspace = root / "solver" / "cases" / spec["id"] / "workspace"
    visible = run_visible_tests(workspace)
    visible_summary = f"{visible['stdout']}\n{visible['stderr']}"
    if visible["returncode"] != 0 or not VISIBLE_TEST_PATTERN.search(visible_summary):
        raise FixtureError(f"covered-control visible tests did not pass for {spec['id']}")
    oracle = run_builtin_oracle(spec, workspace)
    if oracle["returncode"] != 0:
        raise FixtureError(f"covered-control oracle did not pass for {spec['id']}: {oracle['stderr'].strip()}")
    return {
        "case_id": spec["id"],
        "visible": {"returncode": visible["returncode"], "tests_ran": int(VISIBLE_TEST_PATTERN.search(visible_summary).group(1))},
        "oracle": {"returncode": oracle["returncode"]},
    }


def validate_covered_controls(root: Path = DEFAULT_COVERED_CONTROL_ROOT) -> dict[str, Any]:
    """Verify the public freeze and meaningful covered-control behavior gates."""
    verify_covered_spec_shape()
    root = root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise FixtureError(f"covered-control fixture root is missing or unsafe: {root}")
    if (root / "evaluator").exists():
        raise FixtureError("covered-control corpus must not expose evaluator material")
    public_scopes = (root / "README.md", root / "solver")
    assert_manifest(root, "public-sha256.json", public_scopes, "covered-control-freeze")
    freeze = read_json(root / "manifests" / "case-freeze.json")
    if not isinstance(freeze, dict) or freeze.get("format") != FORMAT or freeze.get("freeze_version") != FREEZE_VERSION:
        raise FixtureError("covered-control freeze metadata has an unsupported format")
    if freeze.get("covered_control_case_ids") != list(COVERED_CONTROL_ORDER):
        raise FixtureError("covered-control freeze metadata has unexpected case IDs")
    manifest_path = root / "manifests" / "public-sha256.json"
    if freeze.get("public_manifest_sha256") != sha256_bytes(manifest_path.read_bytes()):
        raise FixtureError("covered-control public manifest digest changed")
    for path in sorted(root.rglob("*.json")):
        assert_canonical_json(path)
    policy_hash = freeze.get("policy_sha256")
    if not isinstance(policy_hash, str):
        raise FixtureError("covered-control policy digest is missing")

    reports = []
    for case_id in COVERED_CONTROL_ORDER:
        spec = covered_spec_by_id(case_id)
        policy = (root / "solver" / "cases" / case_id / "policy" / "REVIEW_POLICY.md").read_bytes()
        if sha256_bytes(policy) != policy_hash:
            raise FixtureError(f"covered-control policy digest changed: {case_id}")
        assert_covered_control_contents(root, spec, policy)
        reports.append(exercise_covered_control(root, spec))
    return {
        "format": FORMAT,
        "root": str(root),
        "frozen": True,
        "covered_control_cases": reports,
    }


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "validate", "prepare-covered-controls", "validate-covered-controls"):
        command = subcommands.add_parser(name)
        default_root = (
            DEFAULT_COVERED_CONTROL_ROOT
            if name.endswith("covered-controls")
            else DEFAULT_ROOT
        )
        command.add_argument("--root", type=Path, default=default_root)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_arguments(arguments)
    try:
        if args.command == "prepare":
            payload = prepare(args.root)
        elif args.command == "validate":
            payload = validate(args.root)
        elif args.command == "prepare-covered-controls":
            payload = prepare_covered_controls(args.root)
        else:
            payload = validate_covered_controls(args.root)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
