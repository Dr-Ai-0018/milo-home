"""Synthetic checks for the structure gate; no database, service, or provider access."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_structure", Path(__file__).with_name("check-structure.py")
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class StructureChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_file_budget_and_exact_generated_exclusion(self):
        large = "// line\n" * 401
        self.write("frontend/src/api/schema.d.ts", large)
        self.write("temp/ignored.ts", large)
        self.write("frontend/src/api/handwritten.ts", "// line\n" * 400)
        self.assertEqual(checker.inspect(self.root)[0], [])
        self.write("frontend/src/api/handwritten.ts", large)
        issues, _ = checker.inspect(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("handwritten.ts", issues[0])

    def test_cli_functions_follow_the_same_budget_as_other_functions(self):
        self.write("backend/src/milo_home/cli.py", "def main():\n" + "    pass\n" * 99)
        issues, notes = checker.inspect(self.root)
        self.assertEqual(issues, [])
        self.assertEqual(notes, [])
        self.write("backend/src/milo_home/cli.py", "def main():\n" + "    pass\n" * 100)
        self.assertTrue(checker.inspect(self.root)[0])
        self.write("backend/src/milo_home/cli.py", "def other():\n" + "    pass\n" * 100)
        self.assertTrue(checker.inspect(self.root)[0])
        self.write("backend/src/milo_home/cli.py", "def other():\n" + "    pass\n" * 99)
        self.assertEqual(checker.inspect(self.root)[0], [])

    def test_lazy_cycle_is_detected_without_importing_modules(self):
        self.write("backend/src/milo_home/a.py", "def f():\n    from . import b\n")
        self.write("backend/src/milo_home/b.py", "import milo_home.a\n")
        issues, _ = checker.inspect(self.root)
        self.assertTrue(any("依赖环" in issue for issue in issues))

    def test_known_edge_does_not_hide_another_cycle(self):
        self.write("backend/src/milo_home/database.py", "from . import database_inspection\n")
        self.write(
            "backend/src/milo_home/database_inspection.py",
            "from . import database\nfrom . import schedules\n",
        )
        self.write("backend/src/milo_home/schedules.py", "from . import database\n")
        issues, notes = checker.inspect(self.root)
        self.assertTrue(notes)
        self.assertEqual(issues, [])
        self.write("backend/src/milo_home/schedules.py", "from . import database_inspection\n")
        issues, notes = checker.inspect(self.root)
        self.assertTrue(notes)
        self.assertTrue(any("依赖环" in issue for issue in issues))

    def test_invalid_python_and_line_endings(self):
        self.write("backend/src/milo_home/broken.py", "def bad(\n")
        self.write("frontend/src/broken.ts", "const value = 1\r\n")
        issues, _ = checker.inspect(self.root)
        self.assertEqual(len(issues), 2)

    def test_acyclic_nested_package_and_external_dependency(self):
        self.write("backend/src/milo_home/area/__init__.py", "from .. import base\n")
        self.write("backend/src/milo_home/base.py", "import sqlite3\n")
        self.assertEqual(checker.inspect(self.root)[0], [])


if __name__ == "__main__":
    unittest.main()
