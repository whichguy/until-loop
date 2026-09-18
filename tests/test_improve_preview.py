"""Compatibility guards for the historical v2 Improve preview harness."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fresh_context  # noqa: E402
import improve_preview  # noqa: E402


def legacy_v2_source(destination: Path) -> Path:
    (destination / "scripts").mkdir(parents=True)
    (destination / "references").mkdir()
    for relative in fresh_context.SNAPSHOT_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for source in (ROOT / "references").glob("*.md"):
        if source.name != "runtime-ephemeral.md":
            shutil.copy2(source, destination / "references" / source.name)
    for relative in improve_preview.EXTRA_SNAPSHOT_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (destination / "SKILL.md").write_text("# Historical v2 card\n", encoding="utf-8")
    return destination


class ImprovePreviewCompatibilityTests(unittest.TestCase):
    def test_prepare_rejects_current_callback_source_before_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            review_root = Path(temporary) / "review"
            with self.assertRaisesRegex(improve_preview.ImprovePreviewError, "current callback-protocol source"):
                improve_preview.prepare(review_root, "current", ROOT, [])
            self.assertFalse(review_root.exists())

    def test_historical_v2_source_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = legacy_v2_source(root / "legacy")
            case_id = next(iter(improve_preview.load_cases()))
            prepared = improve_preview.prepare(root / "review", "legacy", source, [case_id])
            self.assertTrue(prepared.is_file())
