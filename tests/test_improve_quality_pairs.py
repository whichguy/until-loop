"""Focused tests for read-only blinded static pair preparation."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import improve_quality_pairs as pairs


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


class PairPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self, evidence: Path, snapshot_id: str, reason: str,
                 files: dict[str, bytes], stable: bool = True) -> dict:
        candidate = evidence / "snapshots" / snapshot_id / "candidate"
        manifest = {}
        for relative, value in files.items():
            write_bytes(candidate / relative, value)
            manifest[relative] = {"kind": "file", "bytes": len(value), "sha256": digest(value)}
        # These retained runtime and user-work files must never enter a pair.
        write_bytes(candidate / ".git/HEAD", b"ref: refs/heads/main\n")
        write_bytes(candidate / ".until-loop/state.json", b'{"phase":"done"}\n')
        write_bytes(candidate / "notes/user-work.txt", b"private draft\n")
        candidate_digest = digest(b"\0".join(manifest[relative]["sha256"].encode("ascii")
                                                for relative in sorted(manifest)))
        record = {"id": snapshot_id, "candidate_digest": candidate_digest, "stable": stable,
                  "reason": reason, "manifest": manifest}
        write_json(evidence / "snapshots" / snapshot_id / "snapshot.json", record)
        return record

    def trial(self, name: str = "trial", grade_status: str = "pass") -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
        trial = self.root / name
        evidence = trial / "evidence"
        scope = ["README.md", "product.py", "tests/test_product.py"]
        initial_files = {
            "README.md": b"# Product\n\nReturn old values.\n",
            "product.py": b"def render(value):\n    return value.strip()\n",
            "tests/test_product.py": b"def test_trim():\n    assert True\n",
        }
        final_files = {
            "README.md": b"# Product\n\nReturn Anonymous for blank values.\n",
            "product.py": b"def render(value):\n    return value.strip() or 'Anonymous'\n",
            "tests/test_product.py": b"def test_blank():\n    assert True\n",
        }
        initial = self.snapshot(evidence, "s0000", "before invocation", initial_files)
        final = self.snapshot(evidence, "s0001", "after invocation", final_files)
        write_json(trial / "trial.json", {
            "format": "improve-quality/v1",
            "evidence": str(evidence),
            "fixture": {"scope_paths": scope},
        })
        write_json(evidence / "audit-selection.json", {"directory": "audit", "grade_file": "grade.json"})
        write_json(evidence / "audit/observed.json", {"snapshots": [initial, final]})
        write_json(evidence / "audit/grade.json", {
            "status": grade_status,
            "sequence": {
                "final_observed_snapshot_id": "s0001",
                "final_observed_candidate_digest": final["candidate_digest"],
                "final_observed_snapshot_stable": True,
                "final_candidate_bound": True,
            },
        })
        return trial, initial_files, final_files

    def output(self, name: str = "pair-001") -> Path:
        parent = self.root / "pairs"
        parent.mkdir(exist_ok=True)
        return parent / name

    def test_preparation_copies_only_scope_into_opaque_sides_and_preserves_trial(self) -> None:
        trial, initial, final = self.trial()
        before = file_tree(trial)
        result = pairs.prepare_pair(trial, self.output(), seed=17)
        pair_root, sidecar_path = result["pair_root"], result["orientation_path"]
        sidecar = json.loads(sidecar_path.read_text())

        self.assertEqual(sorted(path.name for path in pair_root.iterdir()), ["left", "right"])
        self.assertEqual(sorted(str(path.relative_to(pair_root)) for path in pair_root.rglob("*") if path.is_file()), [
            "left/README.md", "left/product.py", "left/tests/test_product.py",
            "right/README.md", "right/product.py", "right/tests/test_product.py",
        ])
        self.assertFalse((pair_root / "orientation.json").exists())
        self.assertTrue(sidecar["evaluator_only"])
        self.assertEqual(sidecar["original_grade_status"], "pass")
        self.assertIn("do not provide", sidecar["boundary"].lower())
        self.assertEqual(sidecar["scope_paths"], ["README.md", "product.py", "tests/test_product.py"])
        for side, source in sidecar["orientation"].items():
            expected = final if source == "final" else initial
            for relative, value in expected.items():
                copied = pair_root / side / relative
                self.assertEqual(copied.read_bytes(), value)
                self.assertEqual(sidecar["sha256"][side + "/" + relative], digest(value))
        self.assertEqual(before, file_tree(trial))

    def test_orientation_is_deterministic_for_identical_retained_pairs(self) -> None:
        first, _, _ = self.trial("trial-a")
        second, _, _ = self.trial("trial-b")
        first_result = pairs.prepare_pair(first, self.output("pair-a"), seed=4)
        second_result = pairs.prepare_pair(second, self.output("pair-b"), seed=4)
        self.assertEqual(first_result["orientation"], second_result["orientation"])
        self.assertEqual(
            json.loads(first_result["orientation_path"].read_text())["orientation"],
            json.loads(second_result["orientation_path"].read_text())["orientation"],
        )

    def test_incomplete_grade_is_labeled_but_can_supply_a_bound_static_pair(self) -> None:
        trial, _, _ = self.trial(grade_status="incomplete")
        result = pairs.prepare_pair(trial, self.output(), seed=1)
        sidecar = json.loads(result["orientation_path"].read_text())
        self.assertEqual(sidecar["original_grade_status"], "incomplete")
        self.assertTrue(result["pair_root"].is_dir())

    def test_final_binding_mismatch_fails_before_creating_output(self) -> None:
        trial, _, _ = self.trial()
        grade_path = trial / "evidence/audit/grade.json"
        grade = json.loads(grade_path.read_text())
        grade["sequence"]["final_observed_candidate_digest"] = "wrong"
        write_json(grade_path, grade)
        output = self.output()
        before = file_tree(trial)
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())
        self.assertFalse((output.parent / "pair-001-orientation.json").exists())
        self.assertEqual(before, file_tree(trial))

    def test_retained_initial_reason_must_match_audited_before_invocation_reason(self) -> None:
        trial, _, _ = self.trial("reason-mismatch")
        retained_path = trial / "evidence/snapshots/s0000/snapshot.json"
        retained = json.loads(retained_path.read_text())
        retained["reason"] = "after invocation"
        write_json(retained_path, retained)
        output = self.output("pair-reason-mismatch")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())
        self.assertFalse((output.parent / "pair-reason-mismatch-orientation.json").exists())

    def test_manifest_evidence_must_belong_to_the_trial_root(self) -> None:
        trial, _, _ = self.trial("bound-trial")
        sibling, _, _ = self.trial("sibling-trial")
        manifest_path = trial / "trial.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["evidence"] = str(sibling / "evidence")
        write_json(manifest_path, manifest)
        output = self.output("pair-sibling-evidence")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())
        self.assertFalse((output.parent / "pair-sibling-evidence-orientation.json").exists())

    def test_manifest_hash_mismatch_and_symlink_fail_closed(self) -> None:
        trial, _, _ = self.trial("tampered")
        source = trial / "evidence/snapshots/s0001/candidate/product.py"
        source.write_bytes(b"tampered\n")
        output = self.output("pair-tampered")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())

        trial, _, _ = self.trial("linked")
        source = trial / "evidence/snapshots/s0001/candidate/product.py"
        source.unlink()
        source.symlink_to(self.root / "outside.py")
        output = self.output("pair-linked")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())

    def test_unstable_snapshot_and_unsafe_scope_fail_before_output(self) -> None:
        trial, _, _ = self.trial("unstable")
        observed_path = trial / "evidence/audit/observed.json"
        observed = json.loads(observed_path.read_text())
        observed["snapshots"][0]["stable"] = False
        write_json(observed_path, observed)
        output = self.output("pair-unstable")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())

        trial, _, _ = self.trial("unsafe-scope")
        manifest_path = trial / "trial.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["fixture"]["scope_paths"] = ["../outside.py"]
        write_json(manifest_path, manifest)
        output = self.output("pair-unsafe")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertFalse(output.exists())

    def test_existing_pair_or_orientation_never_overwrites(self) -> None:
        trial, _, _ = self.trial()
        output = self.output()
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertEqual(sentinel.read_text(), "keep")

        output = self.output("pair-sidecar")
        sidecar = output.parent / "pair-sidecar-orientation.json"
        sidecar.write_text("keep")
        with self.assertRaises(pairs.PairError):
            pairs.prepare_pair(trial, output, seed=1)
        self.assertEqual(sidecar.read_text(), "keep")
        self.assertFalse(output.exists())

    def test_cli_prepare_reports_paths_without_orientation_mapping(self) -> None:
        trial, _, _ = self.trial()
        output = self.output()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(pairs.main(["prepare", "--trial", str(trial), "--output", str(output), "--seed", "3"]), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["pair_root"], str(output))
        self.assertIn("orientation_path", report)
        self.assertNotIn("orientation", report)


if __name__ == "__main__":
    unittest.main()
