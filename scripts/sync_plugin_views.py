#!/usr/bin/env python3
"""Build or verify the self-contained Until Loop plugin distribution.

The source card and runtime remain authoritative in this repository.  This
script materializes the host-plugin view as ordinary files so it can be copied
or installed without a sibling checkout, symlink, or preinstalled skill.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple


VERSION = "0.5.0"
REPOSITORY_URL = "https://github.com/whichguy/until-loop"
RUNTIME_SCRIPTS = ("scripts/until_loop_ephemeral.py",)
RUNTIME_REFERENCES = ("references/runtime-ephemeral.md",)


class PackagingError(RuntimeError):
    """The generated distribution cannot safely represent its source."""


class PlannedFile(NamedTuple):
    """One regular file in a generated plugin tree."""

    contents: bytes
    mode: int
    source: Optional[str]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_mappings() -> Dict[str, Dict[str, str]]:
    """Return destination-to-canonical-source mappings for the plugin view."""
    until_loop: Dict[str, str] = {
        "LICENSE": "LICENSE",
        "README.md": "docs/until-loop-plugin-readme.md",
        "skills/until-loop/SKILL.md": "SKILL.md",
        "skills/until-loop/agents/openai.yaml": "agents/openai.yaml",
    }
    for source in RUNTIME_SCRIPTS + RUNTIME_REFERENCES:
        until_loop["skills/until-loop/" + source] = source
    return {"until-loop": until_loop}


DESCRIPTION = (
    "Interpret natural-language work and evidence-based exit conditions "
    "with a private callback state file and LLM judgment."
)


def metadata(name: str) -> Tuple[dict, dict]:
    """Return Claude and Codex manifests for the self-contained plugin."""
    if name != "until-loop":
        raise PackagingError("unknown plugin: {}".format(name))
    common = {
        "name": name,
        "version": VERSION,
        "description": DESCRIPTION,
        "author": {"name": "whichguy", "url": "https://github.com/whichguy"},
        "homepage": REPOSITORY_URL,
        "repository": REPOSITORY_URL,
        "license": "MIT",
        "keywords": ["until-loop", "ephemeral-callback", "evidence-based"],
    }
    codex_interface = {
        "displayName": "Until Loop",
        "shortDescription": "Pursue work until its evidence-based exit condition holds",
        "longDescription": DESCRIPTION,
        "developerName": "whichguy",
        "category": "Productivity",
        "capabilities": ["Read", "Write"],
        "defaultPrompt": [
            "Use $until-loop to finish this task and verify its exit condition."
        ],
    }
    codex = dict(common)
    codex["skills"] = "./skills/"
    codex["interface"] = codex_interface
    # Claude discovers skills conventionally.  Keep this manifest to fields
    # supported by Claude's plugin format; Codex-specific interface metadata
    # belongs only in its companion manifest.
    return common, codex


def manifest_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def checked_source(root: Path, relative: str) -> PlannedFile:
    """Read one canonical source as an independently copied regular file."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or str(pure) == ".":
        raise PackagingError("unsafe source path: {}".format(relative))
    current = root
    for component in pure.parts:
        current = current / component
        try:
            item = current.lstat()
        except OSError as error:
            raise PackagingError("cannot inspect source {}: {}".format(relative, error)) from error
        if stat.S_ISLNK(item.st_mode):
            raise PackagingError("source path is a symlink: {}".format(relative))
    if not stat.S_ISREG(item.st_mode):
        raise PackagingError("source is not a regular file: {}".format(relative))
    try:
        contents = current.read_bytes()
    except OSError as error:
        raise PackagingError("cannot read source {}: {}".format(relative, error)) from error
    return PlannedFile(contents, stat.S_IMODE(item.st_mode), relative)


def expected_views(root: Optional[Path] = None) -> Dict[str, Dict[str, PlannedFile]]:
    """Materialize the expected file records without touching ``plugins/``."""
    root = (root or repository_root()).resolve()
    views: Dict[str, Dict[str, PlannedFile]] = {}
    for name, mapping in source_mappings().items():
        files = {
            destination: checked_source(root, source)
            for destination, source in mapping.items()
        }
        claude, codex = metadata(name)
        files[".claude-plugin/plugin.json"] = PlannedFile(
            manifest_bytes(claude), 0o644, None
        )
        files[".codex-plugin/plugin.json"] = PlannedFile(
            manifest_bytes(codex), 0o644, None
        )
        views[name] = files
    return views


def expected_directories(files: Dict[str, PlannedFile]) -> set:
    directories = set()
    for relative in files:
        current = PurePosixPath(relative).parent
        while str(current) != ".":
            directories.add(current.as_posix())
            current = current.parent
    return directories


def actual_entries(root: Path) -> Dict[str, Tuple[str, int, Optional[bytes], int]]:
    """Return all descendants without following a symlink or special file."""
    entries: Dict[str, Tuple[str, int, Optional[bytes], int]] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise PackagingError("cannot inspect generated directory {}: {}".format(directory, error)) from error
        for child in children:
            relative = child.name if not prefix else prefix + "/" + child.name
            try:
                item = child.stat(follow_symlinks=False)
            except OSError as error:
                raise PackagingError("cannot inspect generated path {}: {}".format(relative, error)) from error
            mode = stat.S_IMODE(item.st_mode)
            if stat.S_ISLNK(item.st_mode):
                entries[relative] = ("symlink", mode, None, item.st_nlink)
            elif stat.S_ISDIR(item.st_mode):
                entries[relative] = ("directory", mode, None, item.st_nlink)
                visit(Path(child.path), relative)
            elif stat.S_ISREG(item.st_mode):
                try:
                    contents = Path(child.path).read_bytes()
                except OSError as error:
                    raise PackagingError("cannot read generated path {}: {}".format(relative, error)) from error
                entries[relative] = ("file", mode, contents, item.st_nlink)
            else:
                entries[relative] = ("special", mode, None, item.st_nlink)

    visit(root, "")
    return entries


def check_view(target: Path, files: Dict[str, PlannedFile]) -> List[str]:
    """Return exact tree drift diagnostics for one generated distribution."""
    if target.is_symlink():
        return ["plugin root is a symlink"]
    try:
        target_item = target.lstat()
    except FileNotFoundError:
        return ["plugin root is missing"]
    except OSError as error:
        return ["cannot inspect plugin root: {}".format(error)]
    if not stat.S_ISDIR(target_item.st_mode):
        return ["plugin root is not a directory"]

    actual = actual_entries(target)
    failures: List[str] = []
    expected_dirs = expected_directories(files)
    for relative, (kind, _mode, _contents, _nlink) in sorted(actual.items()):
        if relative not in files and relative not in expected_dirs:
            failures.append("extra path: {}".format(relative))
        elif relative in expected_dirs and kind != "directory":
            failures.append("expected directory: {}".format(relative))
        elif relative in files and kind != "file":
            failures.append("expected independent regular file: {} ({})".format(relative, kind))

    for relative in sorted(expected_dirs):
        if relative not in actual:
            failures.append("missing directory: {}".format(relative))
    for relative, planned in sorted(files.items()):
        record = actual.get(relative)
        if record is None:
            failures.append("missing file: {}".format(relative))
            continue
        kind, mode, contents, nlink = record
        if kind != "file":
            continue
        if nlink != 1:
            failures.append("file is not an independent copy: {}".format(relative))
        if mode != planned.mode:
            failures.append(
                "mode drift: {} ({:04o} != {:04o})".format(relative, mode, planned.mode)
            )
        if contents != planned.contents:
            failures.append("content drift: {}".format(relative))
    return failures


def write_view(staging: Path, files: Dict[str, PlannedFile]) -> None:
    """Create one new tree from the checked source bytes."""
    for relative, planned in sorted(files.items()):
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(planned.contents)
        os.chmod(destination, planned.mode)


def remove_view(target: Path) -> None:
    """Remove only one known plugin root, without following a symlink."""
    if target.is_symlink():
        target.unlink()
        return
    try:
        item = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(item.st_mode):
        shutil.rmtree(target)
    else:
        target.unlink()


def plugin_parent(root: Path, *, create: bool) -> Optional[Path]:
    """Return the repository-local ``plugins`` directory without following it."""
    target = root / "plugins"
    try:
        item = target.lstat()
    except FileNotFoundError:
        if not create:
            return None
        target.mkdir(mode=0o755)
        item = target.lstat()
    except OSError as error:
        raise PackagingError("cannot inspect plugins directory: {}".format(error)) from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise PackagingError("plugins directory must be a real directory")
    return target


def sync(root: Optional[Path] = None) -> Dict[str, int]:
    """Replace the explicitly named generated view from an in-memory plan."""
    root = (root or repository_root()).resolve()
    views = expected_views(root)
    plugin_root = plugin_parent(root, create=True)
    assert plugin_root is not None
    counts: Dict[str, int] = {}
    for name, files in views.items():
        staging_parent = Path(tempfile.mkdtemp(prefix=".sync-{}-".format(name), dir=plugin_root))
        staging = staging_parent / name
        try:
            write_view(staging, files)
            target = plugin_root / name
            remove_view(target)
            os.replace(staging, target)
            counts[name] = len(files)
        finally:
            if staging_parent.exists() and not staging_parent.is_symlink():
                shutil.rmtree(staging_parent)
    return counts


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated plugin views differ from their canonical sources",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = repository_root()
    try:
        if args.check:
            failures: List[str] = []
            views = expected_views(root)
            plugin_root = plugin_parent(root, create=False)
            for name, files in views.items():
                target = (plugin_root / name) if plugin_root is not None else (root / "plugins" / name)
                for failure in check_view(target, files):
                    failures.append("plugins/{}/{}".format(name, failure))
            if failures:
                print("plugin views are stale:", file=sys.stderr)
                for failure in failures:
                    print("- {}".format(failure), file=sys.stderr)
                return 1
            print("plugin views are current")
            return 0
        counts = sync(root)
    except PackagingError as error:
        print("sync_plugin_views: {}".format(error), file=sys.stderr)
        return 2
    for name in sorted(counts):
        print("updated plugins/{} ({} files)".format(name, counts[name]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
