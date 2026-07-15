from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aigame.aseprite import aseprite_export
from aigame.core import WorkflowError


class AsepriteAdapterTests(unittest.TestCase):
    def test_batch_export_is_dry_run_safe_and_requests_layers_tags_and_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "assets/source/hero.aseprite"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fixture")
            result = aseprite_export(root, source, "assets/source/hero-export", apply=False)
            self.assertEqual("dry_run", result["status"])
            self.assertIn("--sheet", result["command"])
            self.assertIn("--data", result["command"])
            self.assertIn("--list-tags", result["command"])
            self.assertIn("--list-slices", result["command"])
            self.assertFalse((root / "assets/source/hero-export.png").exists())

    def test_paths_cannot_escape_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            root.mkdir()
            source = root / "hero.ase"
            source.write_bytes(b"fixture")
            with self.assertRaisesRegex(WorkflowError, "escapes"):
                aseprite_export(root, source, "../outside", apply=False)


if __name__ == "__main__":
    unittest.main()
