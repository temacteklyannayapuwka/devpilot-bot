import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_env, load_projects


class ConfigTests(unittest.TestCase):
    def test_env_is_parsed_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("export DEMO_VALUE='$(echo never-execute)'\n")
            with patch.dict(os.environ, {}, clear=True):
                load_env(path)
                self.assertEqual(os.environ["DEMO_VALUE"], "$(echo never-execute)")

    def test_checks_require_argument_arrays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.json"
            path.write_text(json.dumps({"demo": {"path": directory, "checks": ["echo no"]}}))
            with self.assertRaises(ValueError):
                load_projects(path)
            path.write_text(json.dumps({"demo": {"path": directory, "checks": [["python3", "-V"]]}}))
            self.assertEqual(load_projects(path)["demo"].branch, "main")
