#!/usr/bin/env python3
"""Multi-model council: fan a prompt out to several OpenAI-compatible endpoints in parallel.

Stdlib only -- no venv, no pip.

  council.py --list-models [--provider nvidia]   authoritative model IDs from the API
  council.py --list-roles                        available review roles
  council.py --set-key nvidia                    store a key (reads stdin, never argv)
  council.py --configure                         write panelists from a JSON spec on stdin
  council.py --show                              current panel
  council.py --check                             liveness probe, ~1 token each
  council.py --prompt-file PATH [--only a,b]     ask the panel

Key resolution order: environment variable, then credentials.json (mode 0600).
Keys are never written to config.json and never passed as command-line arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import hashlib
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLED = HERE.parent / "defaults"          # ships with the plugin, read-only

# User data lives outside the plugin so an upgrade never touches keys or config.
COUNCIL_HOME = Path(os.environ.get("COUNCIL_HOME",
                                   Path.home() / ".claude" / "council"))
CONFIG_PATH = COUNCIL_HOME / "config.json"
ROLES_PATH = COUNCIL_HOME / "roles.json"
CREDS_PATH = COUNCIL_HOME / "credentials.json"
HEALTH_PATH = COUNCIL_HOME / "health.json"

# A panelist below this success rate (over at least MIN_ATTEMPTS) is reported
# as unreliable and gets a swap recommendation.
RELIABILITY_FLOOR = 0.80
MIN_ATTEMPTS = 3

# A panelist slower than this on average is unusable in practice even at a
# 100% success rate -- success alone is not reliability.
SLOW_SECONDS = 120

# Substrings marking models that cannot serve as review panelists.
NON_CHAT = ("embed", "rerank", "vision", "image", "ocr", "parse", "speech",
            "translate", "guard", "safety", "reward", "vl-", "-vl", "omni")


# --------------------------------------------------------------------------- io

def ensure_home() -> None:
    """Create the user data dir and seed a config on first run."""
    COUNCIL_HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        example = BUNDLED / "config.example.json"
        if example.exists():
            CONFIG_PATH.write_text(example.read_text())
            print(f"Created {CONFIG_PATH} from defaults. "
                  f"Add a key with --set-key, then run --check.", file=sys.stderr)


def load_json(path: Path, what: str) -> dict:
    if not path.exists():
        sys.exit(f"Missing {what}: {path}")
    try:
        with path.open() as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"Malformed {what} ({path}): {exc}")


def save_json(path: Path, data: dict, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if private:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before it is visible
    tmp.replace(path)


def load_health() -> dict:
    if not HEALTH_PATH.exists():
        return {"panelists": {}}
    try:
        with HEALTH_PATH.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"panelists": {}}


def record_outcomes(results: list) -> None:
    """Append run outcomes so --health can spot degrading panelists.

    Best-effort: a failure to write health data must never break a review.
    """
    try:
        health = load_health()
        for r in results:
            if r.get("error", "").startswith("no key"):
                continue          # a missing key is config, not unreliability
            entry = health["panelists"].setdefault(
                r["name"], {"attempts": 0, "ok": 0, "errors": {},
                            "latencies": [], "model": ""})
            entry["attempts"] += 1
            entry["model"] = r.get("model") or entry.get("model", "")
            if r["ok"]:
                entry["ok"] += 1
                entry["latencies"] = (entry["latencies"] + [r["elapsed"]])[-20:]
            else:
                kind = classify_error(r.get("error", ""))
                entry["errors"][kind] = entry["errors"].get(kind, 0) + 1
                entry["last_error"] = r.get("error", "")[:200]
        save_json(HEALTH_PATH, health)
    except Exception:
        pass


def classify_error(err: str) -> str:
    if "529" in err or "verloaded" in err:
        return "overloaded"
    if "timed out" in err or "timeout" in err.lower():
        return "timeout"
    if "410" in err or "end of life" in err:
        return "retired"
    if "404" in err:
        return "not_found"
    if "403" in err or "401" in err:
        return "auth"
    return "other"


def load_creds() -> dict:
    if not CREDS_PATH.exists():
        return {}
    mode = CREDS_PATH.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"WARNING: {CREDS_PATH} is group/world accessible. "
              f"Fix with: chmod 600 {CREDS_PATH}", file=sys.stderr)
    return load_json(CREDS_PATH, "credentials")


# ------------------------------------------------------------------------ http

def _request(url: str, api_key: str, timeout: int, payload: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------- panel wiring

# Patterns for material that must not be shipped to third-party inference
# providers. Deliberately biased toward false positives: a spurious warning
# costs one --allow-secrets flag, while a missed credential is unrecallable
# once five providers have logged it.
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("NVIDIA key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("connection string with password",
     re.compile(r"\b\w+://[^\s:@/]+:[^\s:@/]+@")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("assigned secret",
     re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|auth)\b"
                r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{12,}")),
]


def _luhn_ok(digits: str) -> bool:
    """Luhn check. Without it, any 13-19 digit run trips the card rule -- and
    order ids, timestamps and account numbers are all long digit runs."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _scan_pii(line: str) -> tuple | None:
    """Card numbers and US SSNs. Not credentials, but equally unrecallable once
    fanned out to five providers, and a review tool meets them in test
    fixtures and sample rows."""
    m = _CARD_RE.search(line)
    if m:
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return ("payment card number", m.group(0))
    m = _SSN_RE.search(line)
    if m:
        return ("US SSN", m.group(0))
    return None


def run_policy_hook(cfg: dict, prompt: str, panel: list) -> tuple:
    """Hand the prompt to an external policy backend before any dispatch.

    The built-in regex scan catches credential SHAPES. It cannot recognise a
    proprietary algorithm, an unreleased strategy, or customer data that looks
    like ordinary text -- and that class of exposure is the one that actually
    matters, because it is invisible to pattern matching by definition.

    Rather than grow a worse detector inside a code-review tool, expose the
    seam. Anything that reads stdin and sets an exit code can sit here: a
    corporate DLP, an off-the-shelf guard, a policy service, or twenty lines of
    grep somebody's security team already trusts.

    Contract, deliberately minimal so it is trivial to implement:
      stdin  -- the exact bytes that would be dispatched
      env    -- COUNCIL_PANEL_SIZE, COUNCIL_PROVIDERS (comma separated)
      exit 0 -- allow
      non-0  -- refuse; stdout/stderr is shown to the user as the reason

    Returns (allowed, reason).
    """
    conf = cfg.get("policy_hook")
    if not conf or not conf.get("command"):
        return True, ""

    cmd = conf["command"]
    if isinstance(cmd, str):
        cmd = [cmd]
    providers = sorted({p.get("provider", "?") for p in panel})
    env = {
        **os.environ,
        "COUNCIL_PANEL_SIZE": str(len(panel)),
        "COUNCIL_PROVIDERS": ",".join(providers),
    }
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=conf.get("timeout", 60), env=env)
    except FileNotFoundError:
        # Fail CLOSED. A policy backend the user configured and that is not
        # there is a broken control, not an absent one -- and this is the one
        # place in the tool where failing open would ship the data anyway.
        return False, (f"policy hook {cmd[0]!r} not found. Fix the command in "
                       f"config.json, or remove policy_hook to disable it.")
    except subprocess.TimeoutExpired:
        return False, f"policy hook timed out after {conf.get('timeout', 60)}s"
    except Exception as exc:
        return False, f"policy hook failed: {type(exc).__name__}: {exc}"

    if r.returncode == 0:
        return True, ""
    reason = (r.stdout or "").strip() or (r.stderr or "").strip()
    return False, reason or f"policy hook exited {r.returncode}"


def scan_for_secrets(text: str) -> list:
    """Return [(line_no, label, redacted_excerpt)] for anything that looks secret.

    The whole point of a council is fan-out: one diff goes to N providers, each
    with its own retention policy, its own jurisdiction, and its own terms. That
    multiplies the blast radius of a credential pasted into a review prompt, and
    unlike a git commit there is no way to un-send it. Refusing by default costs
    a flag; the alternative costs a rotation across five vendors, assuming you
    even notice.
    """
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        hit = _scan_pii(line)
        if hit:
            label, frag = hit
            shown = frag[:6] + "…" + frag[-2:] if len(frag) > 12 else "…"
            findings.append((n, label, shown))
            continue
        for label, pat in SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                frag = m.group(0)
                shown = frag[:6] + "…" + frag[-2:] if len(frag) > 12 else "…"
                findings.append((n, label, shown))
                break
    return findings


def _post_chat(url: str, api_key: str, payload: dict, timeout: int) -> dict:
    """POST a chat completion, adapting once to provider parameter quirks.

    Providers disagree about parameter names and which values are allowed.
    OpenAI's newer models reject `max_tokens` in favour of
    `max_completion_tokens`, and reject a non-default `temperature` outright.
    Every NVIDIA-hosted model accepts the older spelling, so this only bites
    once an OpenAI-direct panelist is seated -- but when it does, the request
    fails 100% of the time and the error is easy to mistake for a bad key.

    Rather than making the user hand-tune per-model config, adapt once from
    the provider's own structured error and retry. A second failure is real
    and propagates.
    """
    try:
        return _request(url, api_key, timeout, payload)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        body = exc.read().decode(errors="replace")
        try:
            err = json.loads(body).get("error", {})
        except json.JSONDecodeError:
            raise urllib.error.HTTPError(url, exc.code, body, exc.headers, None)

        param, code = err.get("param"), err.get("code")
        message = err.get("message", "")
        if code not in ("unsupported_parameter", "unsupported_value"):
            raise urllib.error.HTTPError(url, exc.code, body, exc.headers, None)
        if not param or param not in payload:
            raise urllib.error.HTTPError(url, exc.code, body, exc.headers, None)

        retry = dict(payload)
        if param == "max_tokens" and "max_completion_tokens" in message:
            retry["max_completion_tokens"] = retry.pop("max_tokens")
        else:
            retry.pop(param, None)     # value not allowed: fall back to default
        return _request(url, api_key, timeout, retry)


def lineage_of(panelist: dict) -> str:
    """Which training lineage a panelist belongs to.

    The value of a panel is not five opinions, it is DECORRELATED blind spots.
    Two models sharing a base or overlapping training data miss the same bug in
    the same way and then agree with each other -- and that agreement reads as
    consensus when it is one perspective counted twice. A panel is therefore
    most confidently wrong exactly where its members are most alike, which is
    the opposite of what a review is for.

    Observed here: gpt-oss and nemotron independently asserted that
    `except Exception` catches KeyboardInterrupt. It does not. Two votes, one
    error, and majority logic would have promoted it.

    Explicit `lineage` in config wins. Otherwise derive from the vendor prefix,
    which is a proxy and not ground truth -- published lineage says nothing
    about data overlap or distillation between labs. It separates the cases we
    can see and stays silent about the ones we cannot.
    """
    if panelist.get("lineage"):
        return panelist["lineage"]
    model = panelist.get("model", "")
    if "/" in model:
        return model.split("/")[0].lower()
    return panelist.get("provider", "unknown").lower()


def lineage_groups(panel: list) -> dict:
    """{lineage: [panelist names]} for the enabled panel."""
    groups: dict = {}
    for p in panel:
        groups.setdefault(lineage_of(p), []).append(p["name"])
    return groups


def collapsed_vote_count(panel: list) -> int:
    """Independent perspectives, not seats. Same-lineage panelists count once."""
    return len(lineage_groups(panel))


def provider_of(cfg: dict, panelist: dict) -> dict:
    name = panelist.get("provider")
    provider = cfg.get("providers", {}).get(name)
    if not provider:
        raise KeyError(f"panelist '{panelist['name']}' names unknown provider '{name}'")
    return provider


_KEYCHAIN_CACHE: dict[tuple[str, str], str | None] = {}


def fingerprint(secret: str) -> str:
    """Non-reversible 8-hex-char identity check. Never reveals key material."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def keychain_get(service: str, account: str | None = None) -> str | None:
    """Read a secret from the macOS Keychain. Returns None if unavailable.

    The value is piped straight into the HTTPS request -- it is never logged,
    printed, or written to disk by this script.

    Results are cached per (service, account) for the life of the process:
    without this, --show spawns one blocking `security` subprocess per
    panelist, serially, each with a 10s timeout.
    """
    if sys.platform != "darwin":
        return None
    cache_key = (service, account or os.environ.get("USER", ""))
    if cache_key in _KEYCHAIN_CACHE:
        return _KEYCHAIN_CACHE[cache_key]
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-a", account or os.environ.get("USER", ""), "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        _KEYCHAIN_CACHE[cache_key] = None
        return None
    value = out.stdout.strip() if out.returncode == 0 else ""
    _KEYCHAIN_CACHE[cache_key] = value or None
    return value or None


def resolve_key(provider: dict, creds: dict) -> str | None:
    """Resolution order: environment, then Keychain, then credentials.json."""
    env_var = provider["api_key_env"]
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    service = provider.get("keychain_service")
    if service:
        from_keychain = keychain_get(service, provider.get("keychain_account"))
        if from_keychain:
            return from_keychain
    return creds.get(env_var)


def key_source(provider: dict, creds: dict) -> str:
    """Where the key came from -- for --show. Never returns the key itself."""
    env_var = provider["api_key_env"]
    if os.environ.get(env_var):
        return "env"
    service = provider.get("keychain_service")
    if service and keychain_get(service, provider.get("keychain_account")):
        return "keychain"
    if creds.get(env_var):
        return "file"
    return "NONE"


def resolve_lens(roles: dict, panelist: dict) -> tuple[str, str]:
    """Return (label, lens). An inline `lens` on the panelist wins over the role."""
    if panelist.get("lens"):
        return panelist.get("lens_label", "custom"), panelist["lens"]
    key = panelist.get("role", "generalist")
    role = roles["roles"].get(key)
    if not role:
        raise KeyError(f"panelist '{panelist['name']}' names unknown role '{key}'")
    return role["label"], role["lens"]


def ask(cfg: dict, roles: dict, creds: dict, panelist: dict,
        prompt: str, timeout: int, max_tokens: int | None = None) -> dict:
    """Query one panelist. Never raises -- failures come back as a result dict."""
    name = panelist["name"]
    started = time.monotonic()
    try:
        provider = provider_of(cfg, panelist)
        label, lens = resolve_lens(roles, panelist)
    except KeyError as exc:
        return {"name": name, "ok": False, "error": str(exc)}

    key = resolve_key(provider, creds)
    if not key:
        return {"name": name, "ok": False,
                "error": f"no key: set ${provider['api_key_env']}, add keychain "
                         f"item '{provider.get('keychain_service', '?')}', or run "
                         f"--set-key {panelist['provider']}"}

    payload = {
        "model": panelist["model"],
        "messages": [
            {"role": "system", "content": lens},
            {"role": "user", "content": prompt},
        ],
        "temperature": panelist.get("temperature", 0.2),
        "max_tokens": max_tokens or panelist.get("max_tokens", 4096),
        "stream": False,
    }
    if panelist.get("top_p") is not None:
        payload["top_p"] = panelist["top_p"]
    reserved = {"model", "messages", "stream"}
    for k, v in (panelist.get("extra_body") or {}).items():
        if k in reserved:
            return {"name": name, "ok": False,
                    "error": f"extra_body may not override '{k}' -- it would break "
                             f"the request. Remove it from config.json."}
        payload[k] = v

    url = provider["base_url"].rstrip("/") + "/chat/completions"
    try:
        data = _post_chat(url, key, payload, panelist.get("timeout") or timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        hint = ""
        if exc.code == 404 and not detail.strip():
            hint = (f"  (empty 404 on a {len(prompt)}-char prompt usually means the "
                    f"payload exceeded this model's limit, not a bad model id -- "
                    f"verify with --list-models, then shrink the prompt)")
        elif exc.code == 529:
            hint = "  (provider overloaded -- transient, retry)"
        return {"name": name, "ok": False, "error": f"HTTP {exc.code}: {detail}{hint}"}
    except Exception as exc:
        extra = ""
        if "timed out" in str(exc):
            extra = (f"  (exceeded {panelist.get('timeout') or timeout}s -- raise "
                     f"timeout_seconds, or set a per-panelist \"timeout\")")
        return {"name": name, "ok": False,
                "error": f"{type(exc).__name__}: {exc}{extra}"}

    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError):
        return {"name": name, "ok": False,
                "error": f"unexpected response shape: {json.dumps(data)[:400]}"}

    text = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    if not text and reasoning:
        text = reasoning  # some thinking models answer only in the reasoning channel

    usage = data.get("usage") or {}
    return {
        "name": name, "ok": True, "model": panelist["model"], "label": label,
        "text": text, "reasoning": reasoning if text != reasoning else "",
        "elapsed": round(time.monotonic() - started, 1),
        "tokens": usage.get("total_tokens"),
    }


def select_panel(cfg: dict, only: str | None) -> list[dict]:
    panel = [p for p in cfg["panelists"] if p.get("enabled", True)]
    if only:
        wanted = {n.strip().lower() for n in only.split(",") if n.strip()}
        panel = [p for p in panel if p["name"].lower() in wanted]
        missing = wanted - {p["name"].lower() for p in panel}
        if missing:
            sys.exit(f"Unknown or disabled panelist(s): {', '.join(sorted(missing))}")
    if not panel:
        sys.exit("No enabled panelists. Run --configure.")
    return panel


# -------------------------------------------------------------------- commands

def cmd_set_key(provider_name: str, cfg: dict) -> int:
    provider = cfg.get("providers", {}).get(provider_name)
    if not provider:
        sys.exit(f"Unknown provider '{provider_name}'. "
                 f"Known: {', '.join(cfg.get('providers', {}))}")
    if sys.stdin.isatty():
        print(f"Paste the key for '{provider_name}', then press Enter:", file=sys.stderr)
        key = sys.stdin.readline().strip()   # Enter terminates; read() would need Ctrl-D
    else:
        key = sys.stdin.read().strip()
    if not key:
        sys.exit("No key read from stdin. (Nothing was pasted, or the paste "
                 "did not register.)")
    if any(c.isspace() for c in key):
        sys.exit("Key contains whitespace -- the paste probably picked up extra "
                 "characters. Not stored.")

    creds = load_creds()
    creds[provider["api_key_env"]] = key
    save_json(CREDS_PATH, creds, private=True)
    print(f"Stored under {provider['api_key_env']} in {CREDS_PATH} (mode 0600).")
    print(f"Length {len(key)} chars, fingerprint {fingerprint(key)}. Verify with --check.")
    return 0


def cmd_list_models(cfg: dict, creds: dict, only_provider: str | None,
                    timeout: int) -> int:
    providers = cfg.get("providers", {})
    if only_provider:
        if only_provider not in providers:
            sys.exit(f"Unknown provider '{only_provider}'.")
        providers = {only_provider: providers[only_provider]}

    for pname, provider in providers.items():
        print(f"\n=== {pname}  {provider['base_url']} ===")
        key = resolve_key(provider, creds)
        if not key:
            print(f"  SKIP: no key (set ${provider['api_key_env']} "
                  f"or run --set-key {pname})")
            continue
        try:
            data = _request(provider["base_url"].rstrip("/") + "/models", key, timeout)
        except urllib.error.HTTPError as exc:
            print(f"  ERROR HTTP {exc.code}: "
                  f"{exc.read().decode(errors='replace')[:200]}")
            continue
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}: {exc}")
            continue
        ids = sorted(m.get("id", "?") for m in data.get("data", []))
        print(f"  {len(ids)} models")
        for mid in ids:
            print(f"    {mid}")
    return 0


def cmd_diagnose_key(cfg: dict, provider_name: str) -> int:
    """Explain why a key does or does not resolve. Never prints the key."""
    provider = cfg.get("providers", {}).get(provider_name)
    if not provider:
        sys.exit(f"Unknown provider '{provider_name}'.")

    env_var = provider["api_key_env"]
    service = provider.get("keychain_service")
    user = provider.get("keychain_account") or os.environ.get("USER", "")
    print(f"provider:  {provider_name}")
    print(f"env var:   ${env_var}")
    print(f"keychain:  service='{service}'  account='{user}'")
    print()

    val = os.environ.get(env_var)
    print(f"[1] environment  {'SET (' + str(len(val)) + ' chars)' if val else 'unset'}")

    print("[2] keychain")
    if sys.platform != "darwin":
        print("      skipped: not macOS")
    elif not service:
        print("      skipped: no keychain_service configured")
    elif not user:
        print("      FAIL: $USER is empty, cannot build the query")
    else:
        # Metadata lookup first (no -w) -- this never returns the secret.
        meta = subprocess.run(
            ["security", "find-generic-password", "-a", user, "-s", service],
            capture_output=True, text=True, timeout=10)
        if meta.returncode != 0:
            print(f"      NOT FOUND (exit {meta.returncode})")
            err = (meta.stderr or "").strip()
            if err:
                print(f"      {err}")
            print("      Searching all accounts for this service name...")
            any_acct = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                capture_output=True, text=True, timeout=10)
            if any_acct.returncode == 0:
                for line in (any_acct.stdout or "").splitlines():
                    if '"acct"' in line or '"svce"' in line:
                        print(f"        {line.strip()}")
                print("      -> item exists under a DIFFERENT account than "
                      f"'{user}'. Fix the account or set keychain_account in config.")
            else:
                print(f"      -> no item with service '{service}' in any keychain.")
        else:
            print("      item found")
            secret = subprocess.run(
                ["security", "find-generic-password", "-a", user, "-s", service, "-w"],
                capture_output=True, text=True, timeout=10)
            if secret.returncode != 0:
                print(f"      but read DENIED (exit {secret.returncode}): "
                      f"{(secret.stderr or '').strip()}")
            else:
                v = secret.stdout.strip()
                if not v:
                    print("      readable but EMPTY -- the item was created without a "
                          "value. Delete and re-add it.")
                else:
                    print(f"      readable: {len(v)} chars, "
                          f"fingerprint {fingerprint(v)}")
                    if v != v.strip() or "\n" in v:
                        print("      WARNING: value contains whitespace/newlines; "
                              "this will cause 401s.")

    print("[3] credentials.json")
    creds = load_creds()
    if env_var in creds:
        print(f"      SET ({len(creds[env_var])} chars)")
    else:
        print(f"      not present")
    return 0


def cmd_health(cfg: dict, creds: dict, timeout: int) -> int:
    """Reliability per panelist, with a swap recommendation for weak ones."""
    health = load_health()
    stats = health.get("panelists", {})
    if not stats:
        print("No history yet. Run --check or a review first.")
        return 0

    print(f"{'PANELIST':<12} {'RUNS':>5} {'OK':>5} {'RATE':>6} {'AVG':>7}  ISSUES")
    weak = []
    for pl in cfg["panelists"]:
        name = pl["name"]
        s = stats.get(name)
        if not s or not s["attempts"]:
            print(f"{name:<12} {'-':>5} {'-':>5} {'-':>6} {'-':>7}  no data")
            continue
        rate = s["ok"] / s["attempts"]
        lat = s["latencies"]
        avg = f"{sum(lat)/len(lat):.1f}s" if lat else "-"
        issues = ", ".join(f"{k}x{v}" for k, v in sorted(s["errors"].items())) or "-"
        flag = ""
        avg_s = sum(lat) / len(lat) if lat else 0.0
        if s["attempts"] >= MIN_ATTEMPTS and rate < RELIABILITY_FLOOR:
            flag = "  <-- UNRELIABLE"
            weak.append((name, pl, rate, s, "failures"))
        elif lat and avg_s > SLOW_SECONDS:
            flag = "  <-- TOO SLOW"
            weak.append((name, pl, rate, s, "slow"))
        print(f"{name:<12} {s['attempts']:>5} {s['ok']:>5} {rate*100:>5.0f}% "
              f"{avg:>7}  {issues}{flag}")

    if not weak:
        print("\nAll panelists within tolerance.")
        return 0

    print(f"\n{'=' * 68}\nRECOMMENDATION\n{'=' * 68}")
    for name, pl, rate, s, why in weak:
        if why == "slow":
            lat = s["latencies"]
            avg_s = sum(lat) / len(lat)
            print(f"\n'{name}' ({pl['model']}) answers, but averages "
                  f"{avg_s:.0f}s over {len(lat)} runs -- above the {SLOW_SECONDS}s "
                  f"usable threshold.")
            print("  A panel is only as fast as its slowest member; this one sets "
                  "your whole review latency.")
            if pl.get("extra_body"):
                print("  It runs with extra_body reasoning options. Try lowering "
                      "reasoning_effort before swapping the model out.")
            print(f"  Find faster alternatives:  council.py --suggest-swap {name}")
            continue
        top = max(s["errors"], key=s["errors"].get) if s["errors"] else "unknown"
        print(f"\n'{name}' ({pl['model']}) succeeded {rate*100:.0f}% of "
              f"{s['attempts']} runs. Dominant failure: {top}.")
        if top == "overloaded":
            print("  Provider capacity, not your config. It may recover -- but if the "
                  "panel needs to be dependable, swap it.")
        elif top == "timeout":
            print("  Raise its \"timeout\" in config.json, or swap for a faster model.")
        elif top == "retired":
            print("  This model is gone. It MUST be replaced.")
        elif top == "auth":
            print("  Key problem, not the model. Run --diagnose-key.")
        print(f"  Find replacements:  council.py --suggest-swap {name}")
    return 1


def cmd_suggest_swap(cfg: dict, creds: dict, name: str, timeout: int) -> int:
    """Probe alternative models for a panelist and rank them by measured latency."""
    target = next((p for p in cfg["panelists"] if p["name"] == name), None)
    if not target:
        sys.exit(f"No panelist named '{name}'.")
    provider = provider_of(cfg, target)
    key = resolve_key(provider, creds)
    if not key:
        sys.exit(f"No key for provider '{target['provider']}'.")

    print(f"Current: {name} -> {target['model']} (role: {target.get('role','custom')})")
    print("Fetching catalog...")
    try:
        data = _request(provider["base_url"].rstrip("/") + "/models", key, timeout)
    except Exception as exc:
        sys.exit(f"Could not list models: {exc}")

    in_use = {p["model"] for p in cfg["panelists"]}
    health = load_health()
    dead = set(health.get("unavailable", []))

    cands = [m["id"] for m in data.get("data", [])
             if not any(t in m["id"].lower() for t in NON_CHAT)
             and m["id"] not in in_use and m["id"] not in dead]

    # The catalog lists models the gateway does not actually serve -- they 404
    # with "Function not found". Probing is the only way to know, so bias the
    # sample toward models that look current and away from the long tail of
    # older ones, then remember whatever turns out to be dead.
    seated_vendors = {m.split("/")[0] for m in in_use if "/" in m}
    MODERN = ("nemotron-3", "kimi", "deepseek", "minimax", "qwen3", "gpt-oss",
              "llama-4", "mistral-large", "glm", "phi-4", "granite-3")

    def rank(mid: str) -> tuple:
        low = mid.lower()
        return (
            0 if any(t in low for t in MODERN) else 1,   # current-generation first
            0 if mid.split("/")[0] not in seated_vendors else 1,  # vendor diversity
            mid,
        )

    cands.sort(key=rank)
    cands = cands[:16]
    if not cands:
        sys.exit("No candidate models found.")
    if dead:
        print(f"(skipping {len(dead)} models known to be unavailable)")

    print(f"Probing {len(cands)} candidates (~10 tokens each)...\n")
    probe = "Reply with: ok"
    results = []
    with ThreadPoolExecutor(max_workers=min(len(cands), 6)) as pool:
        futs = {pool.submit(ask, cfg, {"roles": {}}, creds,
                            {**target, "name": mid, "model": mid,
                             "lens": "You are a test probe.", "lens_label": "probe"},
                            probe, 60, 8): mid for mid in cands}
        for f in as_completed(futs):
            results.append(f.result())

    ok = sorted([r for r in results if r["ok"]], key=lambda r: r["elapsed"])
    bad = [r for r in results if not r["ok"]]
    for r in ok:
        vendor = r["model"].split("/")[0]
        note = "  (new vendor)" if vendor not in seated_vendors else ""
        print(f"  OK    {r['elapsed']:>6.1f}s  {r['model']}{note}")
    for r in bad:
        print(f"  FAIL          -  {r['name']}: {r['error'][:80]}")

    # Persist the dead ones so later runs do not pay for them again.
    newly_dead = [r["name"] for r in bad if "404" in r.get("error", "")]
    if newly_dead:
        health.setdefault("unavailable", [])
        health["unavailable"] = sorted(set(health["unavailable"]) | set(newly_dead))
        save_json(HEALTH_PATH, health)
        print(f"\n  ({len(newly_dead)} catalogued but not served -- recorded, "
              f"will be skipped next time)")

    if not ok:
        print("\nNo working replacement found in this sample. Re-run to probe "
              "further into the catalog -- dead models are now skipped.")
        return 1

    if ok:
        best = ok[0]
        print(f"\nSuggested replacement: {best['model']} ({best['elapsed']}s)")
        print("Apply with:\n")
        newpl = [dict(p) for p in cfg["panelists"]]
        for p in newpl:
            if p["name"] == name:
                p["model"] = best["model"]
                p.pop("extra_body", None)   # model-specific; may not apply
        print("  echo '" + json.dumps({"panelists": newpl}) +
              "' | council.py --configure")
    return 0


def cmd_serve(cfg: dict, roles: dict, creds: dict, port: int,
              timeout: int) -> int:
    """Local configuration UI.

    Security posture, since this opens a socket on the user's machine:
      - binds 127.0.0.1 only, never 0.0.0.0
      - every API call must carry a per-run random token
      - Host header is checked, which defeats DNS rebinding
      - shuts down after IDLE_LIMIT seconds with no requests
      - API keys are accepted but never sent back; only fingerprints leave
    """
    import http.server
    import threading
    import webbrowser

    token = secrets.token_urlsafe(24)
    ui_html = (BUNDLED / "ui.html").read_text()
    ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
    IDLE_LIMIT = 1800
    last_seen = [time.monotonic()]

    def current() -> dict:
        """Fresh state each request -- the CLI may have changed things."""
        c = load_json(CONFIG_PATH, "config")
        r = load_json(ROLES_PATH if ROLES_PATH.exists()
                      else BUNDLED / "roles.json", "roles")
        cr = load_creds()
        return {"cfg": c, "roles": r, "creds": cr}

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "council"

        def log_message(self, *a):        # keep the terminal readable
            pass

        def _reject(self, code: int, msg: str):
            body = json.dumps({"error": msg}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            if self.headers.get("Host") not in ALLOWED_HOSTS:
                self._reject(403, "bad Host")      # DNS rebinding
                return False
            if self.headers.get("X-Council-Token") != token:
                self._reject(403, "bad token")
                return False
            last_seen[0] = time.monotonic()
            return True

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                if self.headers.get("Host") not in ALLOWED_HOSTS:
                    return self._reject(403, "bad Host")
                page = ui_html.replace("__TOKEN__", token)
                body = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(body)
                last_seen[0] = time.monotonic()
                return
            if not self._guard():
                return
            st = current()
            if path == "/api/state":
                providers = {}
                for pname, prov in st["cfg"].get("providers", {}).items():
                    key = resolve_key(prov, st["creds"])
                    providers[pname] = {
                        "source": key_source(prov, st["creds"]),
                        "fingerprint": fingerprint(key) if key else None,
                        "length": len(key) if key else 0,
                        "keychain_service": prov.get("keychain_service"),
                    }
                return self._json({
                    "panelists": st["cfg"]["panelists"],
                    "providers": providers,
                    "roles": {k: v["label"] for k, v in st["roles"]["roles"].items()},
                    "role_text": {k: v["lens"] for k, v in st["roles"]["roles"].items()},
                    "config_path": str(CONFIG_PATH),
                })
            if path == "/api/health":
                return self._json(load_health())
            return self._reject(404, "no such endpoint")

        def do_POST(self):
            if not self._guard():
                return
            path = self.path.split("?")[0]
            st = current()
            try:
                data = self._body()
            except Exception as exc:
                return self._reject(400, f"bad body: {exc}")

            if path == "/api/key":
                pname = data.get("provider")
                key = (data.get("key") or "").strip()
                prov = st["cfg"].get("providers", {}).get(pname)
                if not prov:
                    return self._reject(400, "unknown provider")
                if not key:
                    return self._reject(400, "empty key")
                if any(ch.isspace() for ch in key):
                    return self._reject(400, "key contains whitespace")
                cr = load_creds()
                cr[prov["api_key_env"]] = key
                save_json(CREDS_PATH, cr, private=True)
                # Deliberately return only a fingerprint, never the key.
                return self._json({"ok": True, "fingerprint": fingerprint(key),
                                   "length": len(key)})

            if path == "/api/probe":
                pname = data.get("provider", "nvidia")
                prov = st["cfg"].get("providers", {}).get(pname)
                if not prov:
                    return self._reject(400, "unknown provider")
                key = resolve_key(prov, st["creds"])
                if not key:
                    return self._reject(400, f"no key for {pname}")
                try:
                    cat = _request(prov["base_url"].rstrip("/") + "/models",
                                   key, timeout)
                except Exception as exc:
                    return self._reject(502, f"catalog fetch failed: {exc}")

                health = load_health()
                dead = set(health.get("unavailable", []))
                ids = [m["id"] for m in cat.get("data", [])
                       if not any(t in m["id"].lower() for t in NON_CHAT)
                       and m["id"] not in dead]
                ids = ids[:int(data.get("limit", 24))]

                probe_cfg = {"providers": st["cfg"]["providers"]}
                results = []
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futs = [pool.submit(
                        ask, probe_cfg, {"roles": {}}, st["creds"],
                        {"name": mid, "provider": pname, "model": mid,
                         "lens": "You are a test probe.", "lens_label": "probe",
                         "timeout": 60},
                        "Reply with: ok", 60, 8) for mid in ids]
                    for f in as_completed(futs):
                        results.append(f.result())

                newly_dead = [r["name"] for r in results
                              if not r["ok"] and "404" in r.get("error", "")]
                if newly_dead:
                    health.setdefault("unavailable", [])
                    health["unavailable"] = sorted(
                        set(health["unavailable"]) | set(newly_dead))
                    save_json(HEALTH_PATH, health)

                live = sorted(({"model": r["model"], "latency": r["elapsed"]}
                               for r in results if r["ok"]),
                              key=lambda x: x["latency"])
                return self._json({"live": live, "probed": len(ids),
                                   "dead": len(newly_dead),
                                   "skipped": len(dead)})

            if path == "/api/config":
                panelists = data.get("panelists")
                if not isinstance(panelists, list) or not panelists:
                    return self._reject(400, "panelists must be a non-empty list")
                errors = validate_panelists(st["cfg"], st["roles"], panelists)
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                write_panel(st["cfg"], panelists)
                return self._json({"ok": True, "count": len(panelists)})

            if path == "/api/check":
                c = st["cfg"]
                panel = [x for x in c["panelists"] if x.get("enabled", True)]
                if not panel:
                    return self._json({"results": []})
                with ThreadPoolExecutor(max_workers=min(len(panel), 8)) as pool:
                    futs = [pool.submit(ask, c, st["roles"], st["creds"], x,
                                        "Reply with: ok", timeout, 8)
                            for x in panel]
                    res = [f.result() for f in as_completed(futs)]
                record_outcomes(res)
                return self._json({"results": [
                    {"name": r["name"], "ok": r["ok"],
                     "model": r.get("model", ""),
                     "latency": r.get("elapsed"),
                     "error": r.get("error", "")} for r in res]})

            return self._reject(404, "no such endpoint")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_port}/?t={token}"
    # flush: stdout is block-buffered when redirected, and the URL below is
    # the only way to get the token. Without this it never reaches a log file.
    print(f"Council configuration UI: {url}", flush=True)
    print("Bound to 127.0.0.1 only. The token is required and changes every run.",
          flush=True)
    print(f"Auto-stops after {IDLE_LIMIT // 60} min idle. Ctrl-C to stop now.",
          flush=True)

    def watchdog():
        while True:
            time.sleep(15)
            if time.monotonic() - last_seen[0] > IDLE_LIMIT:
                print("\nIdle -- shutting down.")
                httpd.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()

    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_list_roles(roles: dict) -> int:
    for key, role in roles["roles"].items():
        print(f"\n{key}  [{role['label']}]")
        print(f"  {role['lens'][:160]}...")
    return 0


def cmd_show(cfg: dict, roles: dict, creds: dict) -> int:
    print(f"{'PANELIST':<12} {'PROVIDER':<10} {'ROLE':<16} {'KEY':<9} MODEL")
    for p in cfg["panelists"]:
        try:
            provider = provider_of(cfg, p)
            has_key = key_source(provider, creds)
        except KeyError:
            has_key = "?"
        state = "" if p.get("enabled", True) else "  (disabled)"
        role = p.get("role", "-") if not p.get("lens") else "custom"
        print(f"{p['name']:<12} {p.get('provider','?'):<10} {role:<16} "
              f"{has_key:<9} {p['model']}{state}")
    groups = lineage_groups([p for p in cfg["panelists"] if p.get("enabled", True)])
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n{len(groups)} independent lineage(s) across "
          f"{sum(len(v) for v in groups.values())} enabled panelist(s)")
    if dupes:
        print("\nCORRELATED PANELISTS — agreement between these is one "
              "perspective, not two:")
        for lin, names in dupes.items():
            print(f"  {lin}: {', '.join(names)}")
        print("  A panel is most confidently wrong where its members are most "
              "alike.\n  Swap one out, or weight their agreement as a single "
              "vote when synthesising.")
    _ = roles
    return 0


def cmd_configure(cfg: dict, roles: dict) -> int:
    """Read {"panelists":[...]} from stdin and replace the panel."""
    try:
        spec = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.exit(f"Malformed JSON on stdin: {exc}")

    panelists = spec.get("panelists")
    if not isinstance(panelists, list) or not panelists:
        sys.exit('Expected {"panelists": [ ... ]} with at least one entry.')

    errors = validate_panelists(cfg, roles, panelists)
    if errors:
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(f"{len(errors)} problem(s); config not written.")

    if len(panelists) > 8:
        print(f"NOTE: {len(panelists)} panelists. Beyond ~5 the synthesis gets "
              f"noisy and cost scales linearly.", file=sys.stderr)

    write_panel(cfg, panelists)
    print(f"Wrote {len(panelists)} panelists to {CONFIG_PATH}:")
    for pl in panelists:
        print(f"  {pl['name']:<12} {pl.get('role','custom'):<16} {pl['model']}")
    return 0


def write_panel(cfg: dict, panelists: list) -> None:
    cfg["panelists"] = panelists
    save_json(CONFIG_PATH, cfg)


def validate_panelists(cfg: dict, roles: dict, panelists: list) -> list:
    """Pure: returns a list of problems, writes nothing, never exits.

    Shared by --configure and the web UI so the two cannot drift. It must stay
    side-effect free -- it runs inside HTTP handler threads, where sys.exit()
    would kill the connection instead of returning an error to the caller.
    """
    known_providers = set(cfg.get("providers", {}))
    known_roles = set(roles.get("roles", {}))
    seen_names: set = set()
    errors: list = []

    for i, p in enumerate(panelists):
        if not isinstance(p, dict):
            errors.append(f"panelist[{i}]: must be a JSON object, got "
                          f"{type(p).__name__}")
            continue
        for field in ("name", "provider", "model"):
            if not p.get(field):
                errors.append(f"panelist[{i}]: missing '{field}'")
        name = p.get("name", f"[{i}]")
        if name in seen_names:
            errors.append(f"duplicate panelist name '{name}'")
        seen_names.add(name)
        if p.get("provider") and p["provider"] not in known_providers:
            errors.append(f"{name}: unknown provider '{p['provider']}' "
                          f"(known: {', '.join(sorted(known_providers))})")
        role = p.get("role")
        if role and role not in known_roles and not p.get("lens"):
            errors.append(f"{name}: unknown role '{role}' "
                          f"(known: {', '.join(sorted(known_roles))})")
    return errors


def cmd_check(cfg: dict, roles: dict, creds: dict, panel: list[dict],
              timeout: int) -> int:
    failures = 0
    with ThreadPoolExecutor(max_workers=min(len(panel), 8)) as pool:
        futures = {pool.submit(ask, cfg, roles, creds, p, "Reply with: ok",
                               timeout, max_tokens=8): p for p in panel}
        results = [f.result() for f in as_completed(futures)]
    record_outcomes(results)
    for r in sorted(results, key=lambda r: r["name"]):
        if r["ok"]:
            print(f"  PASS  {r['name']:<12} {r['model']}  ({r['elapsed']}s)")
        else:
            failures += 1
            print(f"  FAIL  {r['name']:<12} {r['error']}")
    print(f"\n{len(results) - failures}/{len(results)} reachable")
    return 1 if failures else 0


def cmd_ask(cfg: dict, roles: dict, creds: dict, panel: list[dict],
            prompt: str, timeout: int) -> int:
    with ThreadPoolExecutor(max_workers=min(len(panel), 8)) as pool:
        futures = [pool.submit(ask, cfg, roles, creds, p, prompt, timeout)
                   for p in panel]
        results = [f.result() for f in as_completed(futures)]

    record_outcomes(results)
    order = {p["name"]: i for i, p in enumerate(panel)}
    results.sort(key=lambda r: order.get(r["name"], 999))
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    groups = lineage_groups(panel)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if dupes:
        print(f"{'=' * 72}\nCORRELATED PANELISTS\n{'=' * 72}")
        for lin, names in dupes.items():
            print(f"  {lin}: {', '.join(names)} — count agreement between these "
                  f"as ONE vote")
        print(f"  {collapsed_vote_count(panel)} independent perspective(s), "
              f"{len(panel)} seats.")

    for r in ok:
        lin = lineage_of(next(p for p in panel if p["name"] == r["name"]))
        print(f"\n{'=' * 72}\nPANELIST: {r['name']}  [{r['label']}]  "
              f"lineage: {lin}\n"
              f"model: {r['model']}   elapsed: {r['elapsed']}s   "
              f"tokens: {r['tokens']}\n{'=' * 72}")
        print(r["text"] or "(empty response)")

    if bad:
        print(f"\n{'=' * 72}\nUNAVAILABLE ({len(bad)})\n{'=' * 72}")
        for r in bad:
            print(f"  {r['name']}: {r['error']}")

    print(f"\n--- {len(ok)}/{len(results)} panelists responded ---")
    return 0 if ok else 1


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-model council")
    ap.add_argument("--prompt-file")
    ap.add_argument("--prompt")
    ap.add_argument("--only", help="comma-separated panelist names")
    ap.add_argument("--allow-secrets", action="store_true",
                    help="dispatch even if the prompt looks like it contains "
                         "credentials (default: refuse)")
    ap.add_argument("--provider", help="restrict --list-models to one provider")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--list-roles", action="store_true")
    ap.add_argument("--set-key", metavar="PROVIDER")
    ap.add_argument("--configure", action="store_true",
                    help="read a panelist spec as JSON on stdin")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--diagnose-key", metavar="PROVIDER",
                    help="explain why a key does or does not resolve")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--serve", action="store_true",
                    help="open the local configuration UI in a browser")
    ap.add_argument("--port", type=int, default=8787,
                    help="port for --serve (default 8787)")
    ap.add_argument("--health", action="store_true",
                    help="reliability per panelist + swap recommendations")
    ap.add_argument("--suggest-swap", metavar="PANELIST",
                    help="probe replacement models for a panelist")
    args = ap.parse_args()

    ensure_home()
    cfg = load_json(CONFIG_PATH, "config")
    roles = load_json(ROLES_PATH if ROLES_PATH.exists() else BUNDLED / "roles.json",
                      "roles")
    timeout = cfg.get("timeout_seconds", 180)

    if args.list_roles:
        return cmd_list_roles(roles)
    if args.set_key:
        return cmd_set_key(args.set_key, cfg)
    if args.configure:
        return cmd_configure(cfg, roles)
    if args.diagnose_key:
        return cmd_diagnose_key(cfg, args.diagnose_key)

    creds = load_creds()

    if args.list_models:
        return cmd_list_models(cfg, creds, args.provider, timeout)
    if args.show:
        return cmd_show(cfg, roles, creds)
    if args.serve:
        return cmd_serve(cfg, roles, creds, args.port, timeout)
    if args.health:
        return cmd_health(cfg, creds, timeout)
    if args.suggest_swap:
        return cmd_suggest_swap(cfg, creds, args.suggest_swap, timeout)

    panel = select_panel(cfg, args.only)
    if args.check:
        return cmd_check(cfg, roles, creds, panel, timeout)

    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text()
        except OSError as exc:
            sys.exit(f"Cannot read prompt file: {exc}")
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("Empty prompt.")

    allowed, reason = run_policy_hook(cfg, prompt, panel)
    if not allowed:
        print(f"REFUSED by policy hook.\n\n  {reason}\n", file=sys.stderr)
        return 3

    findings = scan_for_secrets(prompt)
    if findings and not args.allow_secrets:
        print(f"REFUSED: {len(findings)} possible secret(s) in the prompt.\n",
              file=sys.stderr)
        for n, label, shown in findings[:20]:
            print(f"  line {n}: {label}  ({shown})", file=sys.stderr)
        if len(findings) > 20:
            print(f"  ... and {len(findings) - 20} more", file=sys.stderr)
        print(f"\nThis prompt would be sent to {len(panel)} separate providers, "
              f"each with its own\nretention policy and jurisdiction. There is no "
              f"way to un-send it.\n\nRemove the material, or pass --allow-secrets "
              f"if these are false positives.", file=sys.stderr)
        return 2
    if findings:
        print(f"WARNING: dispatching {len(findings)} possible secret(s) to "
              f"{len(panel)} providers (--allow-secrets).", file=sys.stderr)

    return cmd_ask(cfg, roles, creds, panel, prompt, timeout)


if __name__ == "__main__":
    sys.exit(main())
