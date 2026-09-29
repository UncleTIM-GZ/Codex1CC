"""Bundled skill installation tests."""

from pathlib import Path
import tempfile
import unittest

from codex1cc.common import BridgeError
from codex1cc.skill_install import install_skill


class SkillInstallTest(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_modified_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            target = install_skill(root=root)
            skill = target / "SKILL.md"
            self.assertIn("name: codex1cc-ops", skill.read_text(encoding="utf-8"))
            self.assertEqual(install_skill(root=root), target)

            skill.write_text("user changes\n", encoding="utf-8")
            with self.assertRaisesRegex(BridgeError, "--force"):
                install_skill(root=root)
            install_skill(root=root, force=True)
            self.assertIn("name: codex1cc-ops", skill.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
