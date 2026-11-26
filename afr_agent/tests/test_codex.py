import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from afr_agent import codex
from afr_agent.library import RequestError


class CodexAdapterTests(unittest.TestCase):
    def test_structured_calls_explicitly_pin_terra(self):
        def run(argv, **kwargs):
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-terra")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({"ok": True}))
            usage = {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3}
            event = json.dumps({"type": "turn.completed", "usage": usage}).encode()
            return subprocess.CompletedProcess(argv, 0, stdout=event)

        with patch.object(codex.shutil, "which", return_value="/fake/codex"), \
                patch.object(codex.subprocess, "run", side_effect=run):
            output, timing = codex.measured(codex.run_structured, "返回结果", {}, {})
            self.assertEqual(output, {"ok": True})
            self.assertEqual(timing["model"], "gpt-6.1-terra")
            self.assertEqual(timing["usage"], {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3})

    def test_codex_launch_error_returns_a_stable_error_without_exposing_local_details(self):
        with patch.object(codex.shutil, "which", return_value="/fake/codex"), \
                patch.object(codex.subprocess, "run", side_effect=PermissionError("private path")):
            with self.assertRaisesRegex(RequestError, "^codex_unavailable$"):
                codex.run_structured("返回结果", {}, {})
