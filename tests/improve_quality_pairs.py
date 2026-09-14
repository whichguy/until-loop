#!/usr/bin/env python3
"""Prepare blinded, static before/after inputs from retained Improve evidence.

The preparation step is read-only with respect to a trial.  It never invokes a
model or test command, never writes the candidate or evidence, and keeps the
initial/final orientation in an evaluator-only sibling file rather than in the
blinded pair directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any


FORMAT = "improve-quality-blinded-pair/v1"
FORBIDDEN_SCOPE_COMPONENTS = frozenset((
    ".git", ".until-loop", "audit", "evidence", "package", "runtime", "snapshots",
))


class PairError(RuntimeError):
    """Retained evidence cannot safely produce a blinded pair."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def require_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PairError(label + " is missing or unsafe")


def require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PairError(label + " is missing or unsafe")


def read_object(path: Path, label: str) -> dict[str, Any]:
    require_regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PairError(label + " is unreadable") from error
    if not isinstance(value, dict):
        raise PairError(label + " must be a JSON object")
    return value


def safe_relative(value: Any, label: str) -> Path:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise PairError(label + " is not a safe relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise PairError(label + " is not a safe relative path")
    return Path(*relative.parts)


def safe_snapshot_id(value: Any, label: str) -> str:
    relative = safe_relative(value, label)
    if len(relative.parts) != 1:
        raise PairError(label + " is not a safe snapshot identifier")
    return relative.name


def scoped_paths(manifest: dict[str, Any]) -> list[Path]:
    fixture = manifest.get("fixture")
    values = fixture.get("scope_paths") if isinstance(fixture, dict) else None
    if not isinstance(values, list) or not values:
        raise PairError("trial manifest has no scoped files")
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        relative = safe_relative(value, "scoped path")
        if any(part.startswith(".") or part in FORBIDDEN_SCOPE_COMPONENTS for part in relative.parts):
            raise PairError("scoped path includes evaluator or runtime metadata")
        key = relative.as_posix()
        if key in seen:
            raise PairError("trial manifest repeats a scoped path")
        seen.add(key)
        result.append(relative)
    return result


def safe_child(root: Path, relative: Path, label: str) -> Path:
    """Join a validated relative path without traversing a symlinked component."""
    require_directory(root, label + " root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PairError(label + " traverses a symlink")
    return current


def manifest_item(snapshot: dict[str, Any], relative: Path, label: str) -> dict[str, Any]:
    values = snapshot.get("manifest")
    if not isinstance(values, dict):
        raise PairError(label + " has no manifest")
    item = values.get(relative.as_posix())
    if not isinstance(item, dict) or item.get("kind") != "file":
        raise PairError(label + " does not retain scoped file " + relative.as_posix())
    checksum = item.get("sha256")
    size = item.get("bytes")
    if (not isinstance(checksum, str) or len(checksum) != 64 or
            any(character not in "0123456789abcdef" for character in checksum) or
            type(size) is not int or size < 0):
        raise PairError(label + " has an invalid manifest entry for " + relative.as_posix())
    return item


def require_snapshot_binding(observed: dict[str, Any], retained: dict[str, Any],
                             snapshot_id: str, scope: list[Path], label: str,
                             expected_reason: str | None = None) -> None:
    if observed.get("id") != snapshot_id or retained.get("id") != snapshot_id:
        raise PairError(label + " snapshot identity does not match retained evidence")
    if observed.get("stable") is not True or retained.get("stable") is not True:
        raise PairError(label + " snapshot is not stable")
    observed_reason = observed.get("reason")
    retained_reason = retained.get("reason")
    if (type(observed_reason) is not str or type(retained_reason) is not str or
            observed_reason != retained_reason):
        raise PairError(label + " snapshot reason does not match retained evidence")
    if expected_reason is not None and retained_reason != expected_reason:
        raise PairError(label + " retained snapshot has an unexpected reason")
    digest = observed.get("candidate_digest")
    if not isinstance(digest, str) or not digest or retained.get("candidate_digest") != digest:
        raise PairError(label + " snapshot candidate digest is not bound")
    for relative in scope:
        observed_item = manifest_item(observed, relative, label + " observed")
        retained_item = manifest_item(retained, relative, label + " retained")
        for field in ("kind", "sha256", "bytes"):
            if observed_item.get(field) != retained_item.get(field):
                raise PairError(label + " scoped manifest does not match retained snapshot")


def snapshot_catalog(observed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = observed.get("snapshots")
    if not isinstance(values, list):
        raise PairError("selected audit observation has no snapshot list")
    result: dict[str, dict[str, Any]] = {}
    for snapshot in values:
        if not isinstance(snapshot, dict):
            raise PairError("selected audit observation has an invalid snapshot")
        snapshot_id = safe_snapshot_id(snapshot.get("id"), "selected audit snapshot")
        if snapshot_id in result:
            raise PairError("selected audit observation repeats a snapshot ID")
        result[snapshot_id] = snapshot
    return result


def selected_evidence(trial: Path) -> tuple[dict[str, Any], Path, Path, dict[str, Any], Path, dict[str, Any], Path]:
    require_directory(trial, "trial root")
    manifest_path = trial / "trial.json"
    manifest = read_object(manifest_path, "trial manifest")
    evidence_value = manifest.get("evidence")
    if type(evidence_value) is not str or not evidence_value:
        raise PairError("trial manifest has no evidence directory")
    expected_evidence = trial / "evidence"
    require_directory(expected_evidence, "trial evidence")
    manifest_evidence = Path(evidence_value)
    if manifest_evidence.is_symlink():
        raise PairError("trial manifest evidence is unsafe")
    try:
        if manifest_evidence.resolve(strict=True) != expected_evidence.resolve(strict=True):
            raise PairError("trial manifest evidence does not belong to this trial root")
    except OSError as error:
        raise PairError("trial manifest evidence is missing or unsafe") from error
    evidence = expected_evidence

    selection = read_object(evidence / "audit-selection.json", "audit selection")
    audit_relative = safe_relative(selection.get("directory"), "selected audit directory")
    audit = safe_child(evidence, audit_relative, "selected audit directory")
    require_directory(audit, "selected audit directory")
    grade_relative = safe_relative(selection.get("grade_file", "grade.json"), "selected grade file")
    grade_path = safe_child(audit, grade_relative, "selected grade file")
    grade = read_object(grade_path, "selected grade")
    observed_path = grade_path.parent / "observed.json"
    require_regular(observed_path, "selected audit observation")
    observed = read_object(observed_path, "selected audit observation")
    return manifest, manifest_path, evidence, grade, grade_path, observed, observed_path


def initial_and_final(observed: dict[str, Any], grade: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    catalog = snapshot_catalog(observed)
    initial = [snapshot for snapshot in catalog.values() if snapshot.get("reason") == "before invocation"]
    if len(initial) != 1:
        raise PairError("selected audit must retain exactly one initial snapshot")
    initial_snapshot = initial[0]
    if initial_snapshot.get("stable") is not True:
        raise PairError("initial snapshot is not stable")

    sequence = grade.get("sequence")
    if not isinstance(sequence, dict):
        raise PairError("selected grade has no sequence")
    grade_status = grade.get("status")
    if grade_status not in ("pass", "fail", "incomplete"):
        raise PairError("selected grade has no terminal status")
    final_id = safe_snapshot_id(sequence.get("final_observed_snapshot_id"), "final observed snapshot")
    final_snapshot = catalog.get(final_id)
    if not isinstance(final_snapshot, dict):
        raise PairError("selected grade final snapshot is unavailable")
    if sequence.get("final_candidate_bound") is not True:
        raise PairError("selected grade does not bind the final candidate")
    if sequence.get("final_observed_snapshot_stable") is not True or final_snapshot.get("stable") is not True:
        raise PairError("final observed snapshot is not stable")
    final_digest = sequence.get("final_observed_candidate_digest")
    if (not isinstance(final_digest, str) or not final_digest or
            final_snapshot.get("candidate_digest") != final_digest):
        raise PairError("selected grade final candidate digest does not match its snapshot")
    # A terminal incomplete semantic grade remains a valid source for a static
    # pair if this retained candidate binding is complete.  The sidecar records
    # that original status; the pair is not promoted to a semantic pass.
    return initial_snapshot, final_snapshot, grade_status


def read_snapshot_files(evidence: Path, snapshot: dict[str, Any], scope: list[Path], label: str) -> tuple[dict[str, bytes], Path]:
    snapshot_id = safe_snapshot_id(snapshot.get("id"), label + " snapshot")
    snapshots_root = evidence / "snapshots"
    require_directory(snapshots_root, "retained snapshots")
    snapshot_root = safe_child(snapshots_root, Path(snapshot_id), label + " snapshot")
    require_directory(snapshot_root, label + " snapshot")
    record_path = snapshot_root / "snapshot.json"
    retained = read_object(record_path, label + " snapshot record")
    require_snapshot_binding(snapshot, retained, snapshot_id, scope, label,
                             expected_reason="before invocation" if label == "initial" else None)
    candidate = snapshot_root / "candidate"
    require_directory(candidate, label + " candidate snapshot")

    files: dict[str, bytes] = {}
    for relative in scope:
        source = safe_child(candidate, relative, label + " scoped candidate file")
        require_regular(source, label + " scoped candidate file")
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PairError(label + " scoped candidate file is not regular")
        item = manifest_item(snapshot, relative, label + " observed")
        data = source.read_bytes()
        if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
            raise PairError(label + " scoped candidate file does not match its manifest")
        files[relative.as_posix()] = data
    return files, record_path


def deterministic_orientation(seed: int, initial: dict[str, Any], final: dict[str, Any]) -> dict[str, str]:
    if type(seed) is not int:
        raise PairError("seed must be an integer")
    material = "\0".join((FORMAT, str(seed), str(initial["candidate_digest"]), str(final["candidate_digest"])))
    if hashlib.sha256(material.encode("utf-8")).digest()[0] & 1:
        return {"left": "final", "right": "initial"}
    return {"left": "initial", "right": "final"}


def output_paths(output: Path) -> tuple[Path, Path]:
    if output.name in ("", ".", ".."):
        raise PairError("output pair root has no usable name")
    parent = output.parent
    require_directory(parent, "output parent")
    if exists_or_link(output):
        raise PairError("output pair root already exists")
    orientation = parent / (output.name + "-orientation.json")
    if exists_or_link(orientation):
        raise PairError("output orientation file already exists")
    return output, orientation


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise PairError("output orientation file already exists") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def write_pair(output: Path, orientation_path: Path, orientation: dict[str, str],
               initial_files: dict[str, bytes], final_files: dict[str, bytes],
               sidecar: dict[str, Any]) -> None:
    side_sources = {"left": final_files if orientation["left"] == "final" else initial_files,
                    "right": final_files if orientation["right"] == "final" else initial_files}
    created = False
    try:
        try:
            output.mkdir(mode=0o700)
        except FileExistsError as error:
            raise PairError("output pair root already exists") from error
        created = True
        for side in ("left", "right"):
            for relative, data in side_sources[side].items():
                destination = output / side / Path(relative)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(data)
                if sha256_file(destination) != sidecar["sha256"][side + "/" + relative]:
                    raise PairError("copied blinded file does not match retained hash")
        write_new_json(orientation_path, sidecar)
    except Exception:
        if created:
            shutil.rmtree(output, ignore_errors=True)
        raise


def prepare_pair(trial: Path, output: Path, seed: int) -> dict[str, Any]:
    """Create a new opaque pair and sibling evaluator-only orientation record."""
    trial = Path(trial)
    output = Path(output)
    manifest, manifest_path, evidence, grade, grade_path, observed, observed_path = selected_evidence(trial)
    scope = scoped_paths(manifest)
    initial, final, grade_status = initial_and_final(observed, grade)
    initial_files, initial_record = read_snapshot_files(evidence, initial, scope, "initial")
    final_files, final_record = read_snapshot_files(evidence, final, scope, "final")
    orientation = deterministic_orientation(seed, initial, final)
    pair_root, orientation_path = output_paths(output)

    hashes: dict[str, str] = {}
    for side, source in (("left", final_files if orientation["left"] == "final" else initial_files),
                         ("right", final_files if orientation["right"] == "final" else initial_files)):
        for relative, data in source.items():
            hashes[side + "/" + relative] = sha256_bytes(data)
    sidecar = {
        "format": FORMAT,
        "evaluator_only": True,
        "boundary": "This orientation, provenance, and hash record is evaluator-only; do not provide it to a blinded reviewer.",
        "pair_root": str(pair_root),
        "trial_root": str(trial),
        "seed": seed,
        "orientation": orientation,
        "snapshots": {"initial": initial["id"], "final": final["id"]},
        "original_grade_status": grade_status,
        "scope_paths": [relative.as_posix() for relative in scope],
        "sha256": hashes,
        "provenance": {
            "trial_manifest_sha256": sha256_file(manifest_path),
            "selected_audit_observed_sha256": sha256_file(observed_path),
            "selected_grade_sha256": sha256_file(grade_path),
            "initial_snapshot_record_sha256": sha256_file(initial_record),
            "final_snapshot_record_sha256": sha256_file(final_record),
        },
    }
    write_pair(pair_root, orientation_path, orientation, initial_files, final_files, sidecar)
    return {"pair_root": pair_root, "orientation_path": orientation_path, "orientation": orientation,
            "sidecar": sidecar}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare a new blinded static pair")
    prepare.add_argument("--trial", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_pair(args.trial, args.output, args.seed)
    except PairError as error:
        parser.error(str(error))
    print(json.dumps({
        "pair_root": str(result["pair_root"]),
        "orientation_path": str(result["orientation_path"]),
        "boundary": "Orientation remains evaluator-only in the sibling file.",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
