#!/usr/bin/env python3
"""Offline tests. No network, no API keys, no real Keychain access."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "council.py"


def run(args, home, stdin=None):
    """Run the CLI against a throwaway COUNCIL_HOME.

    Clearing the env vars is not enough: key resolution falls through to the
    macOS Keychain, so a developer with a real key configured would have the
    suite make live API calls -- slow, billable, and dependent on someone
    else's uptime. Point keychain_service at a name that cannot exist so the
    lookup always misses.
    """
    env = {**os.environ, "COUNCIL_HOME": str(home)}
    env.pop("NVIDIA_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    cfg = Path(home) / "config.json"
    if cfg.exists():
        c = json.loads(cfg.read_text())
        changed = False
        for prov in c.get("providers", {}).values():
            if prov.get("keychain_service") != "council-test-no-such-service":
                prov["keychain_service"] = "council-test-no-such-service"
                changed = True
        if changed:
            cfg.write_text(json.dumps(c, indent=2) + "\n")
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


class SecretPreflightTests(unittest.TestCase):
    """Fan-out multiplies the blast radius of a leaked credential.

    One diff goes to N providers, each with its own retention policy and
    jurisdiction, and unlike a git commit there is no way to un-send it. The
    scanner is deliberately biased toward false positives: a spurious warning
    costs one flag, a missed credential costs a rotation across every vendor --
    assuming you even notice.
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("council", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_catches_common_credential_shapes(self):
        cases = {
            "AKIAIOSFODNN7EXAMPLE": "AWS access key",
            "sk-proj-AbCdEf0123456789XyZaBcDe": "OpenAI key",
            "nvapi-AbCdEf0123456789XyZaBcDeFg": "NVIDIA key",
            "ghp_AbCdEf0123456789XyZaBcDeFgHi": "GitHub token",
            "-----BEGIN RSA PRIVATE KEY-----": "private key",
            "postgres://admin:hunter2@db:5432/x": "connection string with password",
            'password = "correcthorsebattery"': "assigned secret",
        }
        for text, label in cases.items():
            found = self.m.scan_for_secrets(text)
            self.assertTrue(found, f"missed: {label} in {text[:30]}")
            self.assertEqual(found[0][1], label, f"mislabelled {text[:30]}")

    def test_reports_the_line_number(self):
        text = "clean\nalso clean\nAKIAIOSFODNN7EXAMPLE\n"
        self.assertEqual(self.m.scan_for_secrets(text)[0][0], 3)

    def test_never_echoes_the_secret_in_full(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        _, _, shown = self.m.scan_for_secrets(secret)[0]
        self.assertNotIn(secret, shown)
        self.assertLess(len(shown), len(secret))

    def test_catches_pii_not_just_credentials(self):
        """A review tool meets card numbers and SSNs in test fixtures and sample
        rows. They are not credentials, but they are equally unrecallable once
        fanned out to five providers."""
        self.assertEqual(self.m.scan_for_secrets("4111 1111 1111 1111")[0][1],
                         "payment card number")
        self.assertEqual(self.m.scan_for_secrets("SSN 123-45-6789")[0][1],
                         "US SSN")
        self.assertEqual(
            self.m.scan_for_secrets("Bearer abcdefghijklmnopqrstuvwxyz123456")[0][1],
            "bearer token")

    def test_luhn_keeps_long_digit_runs_from_tripping_the_card_rule(self):
        """Without Luhn, every order id, timestamp and account number is a card
        number and the scan becomes noise people disable."""
        for benign in ("order_id = 1234567890123456",
                       "timestamp 20260826120000123",
                       "seq 9999999999999999"):
            self.assertEqual(self.m.scan_for_secrets(benign), [],
                             f"false positive: {benign}")

    def test_ordinary_code_is_not_flagged(self):
        code = ("def add(a, b):\n    return a + b\n"
                "# token = the auth token is validated upstream\n"
                "api_key = os.environ['API_KEY']\n"
                "self.assertEqual(status, 200)\n")
        self.assertEqual(self.m.scan_for_secrets(code), [],
                         "false positive on ordinary code")

    def test_dispatch_is_refused_when_a_secret_is_present(self):
        import tempfile, os
        home = Path(tempfile.mkdtemp()) / "council"
        run(["--show"], home)
        p = Path(tempfile.mktemp(suffix=".txt"))
        p.write_text("review this\nAKIAIOSFODNN7EXAMPLE\n")
        r = run(["--prompt-file", str(p)], home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("REFUSED", r.stdout + r.stderr)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", r.stdout + r.stderr)


class LineageTests(unittest.TestCase):
    """A panel's value is decorrelated blind spots, not headcount. Same-lineage
    panelists miss the same bug the same way and then agree, which naive
    majority logic promotes -- so the panel is most confidently wrong exactly
    where it is least informative."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("council", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_lineage_comes_from_the_vendor_prefix(self):
        self.assertEqual(
            self.m.lineage_of({"model": "openai/gpt-oss-120b"}), "openai")
        self.assertEqual(
            self.m.lineage_of({"model": "deepseek-ai/deepseek-v4-pro"}), "deepseek-ai")

    def test_a_direct_model_falls_back_to_its_provider(self):
        self.assertEqual(
            self.m.lineage_of({"model": "gpt-5.2", "provider": "openai"}), "openai")

    def test_an_explicit_lineage_overrides_the_prefix(self):
        """Vendor prefix is a proxy. A operator who knows two models share a
        base must be able to say so."""
        self.assertEqual(
            self.m.lineage_of({"model": "someco/llama-derived", "lineage": "meta"}),
            "meta")

    def test_two_openai_models_collapse_to_one_vote(self):
        """The exact flaw that shipped in the default panel: gpt-5.2 and
        gpt-oss-120b are both OpenAI lineage, so six seats were five
        perspectives."""
        panel = [
            {"name": "gpt", "model": "gpt-5.2", "provider": "openai"},
            {"name": "gptoss", "model": "openai/gpt-oss-120b", "provider": "nvidia"},
            {"name": "kimi", "model": "moonshotai/kimi-k3", "provider": "nvidia"},
        ]
        self.assertEqual(self.m.collapsed_vote_count(panel), 2)
        self.assertEqual(sorted(self.m.lineage_groups(panel)["openai"]),
                         ["gpt", "gptoss"])

    def test_a_fully_diverse_panel_collapses_to_nothing(self):
        panel = [{"name": n, "model": f"{v}/m", "provider": "nvidia"}
                 for n, v in [("a", "deepseek-ai"), ("b", "moonshotai"),
                              ("c", "nvidia"), ("d", "minimaxai")]]
        self.assertEqual(self.m.collapsed_vote_count(panel), 4)


class PolicyHookTests(unittest.TestCase):
    """The regex scan catches credential shapes. It cannot recognise a
    proprietary algorithm or an unreleased strategy, and that class of exposure
    is invisible to pattern matching by definition. The hook exists so the tool
    does not have to grow a worse detector than the ones security teams already
    run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "council"
        run(["--show"], self.home)
        self.hook = Path(self.tmp.name) / "hook.sh"

    def tearDown(self):
        self.tmp.cleanup()

    def _set_hook(self, body, timeout=30):
        self.hook.write_text("#!/bin/bash\n" + body + "\n")
        self.hook.chmod(0o755)
        cfg = self.home / "config.json"
        c = json.loads(cfg.read_text())
        c["policy_hook"] = {"command": [str(self.hook)], "timeout": timeout}
        cfg.write_text(json.dumps(c, indent=2) + "\n")

    def _prompt(self, text="review this\n"):
        p = Path(self.tmp.name) / "p.txt"
        p.write_text(text)
        return str(p)

    def test_a_refusing_hook_blocks_dispatch_and_gives_the_reason(self):
        self._set_hook('echo "unreleased strategy may not leave the building"; exit 1')
        r = run(["--prompt-file", self._prompt()], self.home)
        self.assertEqual(r.returncode, 3)
        self.assertIn("unreleased strategy", r.stdout + r.stderr)

    def test_the_hook_receives_the_exact_prompt_on_stdin(self):
        marker = "CANARY-8fd21"
        out = Path(self.tmp.name) / "seen.txt"
        self._set_hook(f'cat > {out}; exit 1')
        run(["--prompt-file", self._prompt(f"review {marker}\n")], self.home)
        self.assertIn(marker, out.read_text())

    def test_a_missing_hook_fails_closed(self):
        """A configured backend that is not there is a broken control, not an
        absent one. Failing open here would ship the data the hook exists to
        withhold."""
        cfg = self.home / "config.json"
        c = json.loads(cfg.read_text())
        c["policy_hook"] = {"command": ["/nonexistent/guard-binary"]}
        cfg.write_text(json.dumps(c, indent=2) + "\n")
        r = run(["--prompt-file", self._prompt()], self.home)
        self.assertEqual(r.returncode, 3)
        self.assertIn("not found", r.stdout + r.stderr)

    def test_a_hanging_hook_fails_closed(self):
        self._set_hook("sleep 30", timeout=1)
        r = run(["--prompt-file", self._prompt()], self.home)
        self.assertEqual(r.returncode, 3)
        self.assertIn("timed out", r.stdout + r.stderr)

    def test_no_hook_configured_is_not_an_error(self):
        r = run(["--prompt-file", self._prompt()], self.home)
        self.assertNotEqual(r.returncode, 3, "absent hook must not block")


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
