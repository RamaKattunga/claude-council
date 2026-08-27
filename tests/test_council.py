#!/usr/bin/env python3
"""Offline tests. No network, no API keys, no real Keychain access."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "council.py"


def run(args, home, stdin=None):
    env = {**os.environ, "COUNCIL_HOME": str(home)}
    env.pop("NVIDIA_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


class CouncilTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "council"

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeds_config_on_first_run(self):
        r = run(["--show"], self.home)
        self.assertTrue((self.home / "config.json").exists())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rejects_non_object_panelist(self):
        run(["--show"], self.home)
        r = run(["--configure"], self.home, stdin='{"panelists":["nope"]}')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be a JSON object", r.stdout + r.stderr)

    def test_rejects_unknown_provider_and_role(self):
        run(["--show"], self.home)
        spec = '{"panelists":[{"name":"a","provider":"zzz","model":"m","role":"qqq"}]}'
        r = run(["--configure"], self.home, stdin=spec)
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertIn("unknown provider", out)
        self.assertIn("unknown role", out)

    def test_rejects_duplicate_names(self):
        run(["--show"], self.home)
        spec = ('{"panelists":[{"name":"a","provider":"nvidia","model":"m","role":"security"},'
                '{"name":"a","provider":"nvidia","model":"n","role":"security"}]}')
        r = run(["--configure"], self.home, stdin=spec)
        self.assertIn("duplicate", (r.stdout + r.stderr).lower())

    def test_missing_prompt_file_exits_cleanly(self):
        run(["--show"], self.home)
        r = run(["--prompt-file", "/definitely/not/here.txt"], self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Cannot read prompt file", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_set_key_rejects_whitespace(self):
        run(["--show"], self.home)
        r = run(["--set-key", "nvidia"], self.home, stdin="key with spaces\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("whitespace", (r.stdout + r.stderr).lower())

    def test_set_key_stores_0600_and_hides_key(self):
        run(["--show"], self.home)
        secret = "nvapi-" + "x" * 64
        r = run(["--set-key", "nvidia"], self.home, stdin=secret + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        creds = self.home / "credentials.json"
        self.assertEqual(oct(creds.stat().st_mode)[-3:], "600")
        self.assertNotIn(secret, r.stdout + r.stderr)      # never echoed
        self.assertIn("fingerprint", r.stdout)
        self.assertEqual(json.loads(creds.read_text())["NVIDIA_API_KEY"], secret)

    def test_extra_body_cannot_override_reserved_fields(self):
        run(["--show"], self.home)
        spec = ('{"panelists":[{"name":"x","provider":"nvidia","model":"m",'
                '"role":"security","extra_body":{"messages":[]}}]}')
        self.assertEqual(run(["--configure"], self.home, stdin=spec).returncode, 0)
        run(["--set-key", "nvidia"], self.home, stdin="nvapi-" + "y" * 64 + "\n")
        r = run(["--check"], self.home)
        self.assertIn("may not override", r.stdout + r.stderr)

    def test_no_key_reports_cleanly(self):
        run(["--show"], self.home)
        r = run(["--check"], self.home)
        self.assertIn("no key", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_roles_fall_back_to_bundled_defaults(self):
        r = run(["--list-roles"], self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        for role in ("correctness", "security", "regression"):
            self.assertIn(role, r.stdout)


class ValidatorContractTests(unittest.TestCase):
    """validate_panelists runs inside HTTP handler threads, so it must be pure:
    return problems, write nothing, never call sys.exit. A previous refactor
    left the write-and-exit tail inside it, which killed the web UI's
    connection instead of returning an error. The CLI tests all still passed,
    so this contract needs its own test."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("council", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.cfg = {"providers": {"nvidia": {"base_url": "x", "api_key_env": "K"}},
                    "panelists": [{"name": "keep", "provider": "nvidia",
                                   "model": "m", "role": "security"}]}
        self.roles = {"roles": {"security": {"label": "s", "lens": "l"}}}

    def test_returns_errors_without_exiting(self):
        bad = [{"name": "a", "provider": "ghost", "model": "m", "role": "ghost"}]
        errs = self.m.validate_panelists(self.cfg, self.roles, bad)   # must not raise
        self.assertEqual(len(errs), 2)

    def test_does_not_mutate_config(self):
        good = [{"name": "new", "provider": "nvidia", "model": "m2",
                 "role": "security"}]
        self.assertEqual(self.m.validate_panelists(self.cfg, self.roles, good), [])
        self.assertEqual(self.cfg["panelists"][0]["name"], "keep")   # untouched

    def test_valid_panel_returns_empty_list(self):
        good = [{"name": "a", "provider": "nvidia", "model": "m", "role": "security"}]
        self.assertEqual(self.m.validate_panelists(self.cfg, self.roles, good), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
