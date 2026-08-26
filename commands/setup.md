---
description: Configure the council - store an API key, discover working models, pick panelists and roles
---

Walk the user through council setup. Be concrete and do the work; do not just print instructions.

**1. Key.** NVIDIA issues keys only through the web UI at <https://build.nvidia.com> (any model
page, "Get API Key") - there is no key-mint API, so this step cannot be automated. One
`nvapi-...` key is account-level and works for **every** model; the user does not need one per
model. Never ask for or handle their password. If they want, open the page in their existing
logged-in Chrome session.

Tell them to run this themselves, so the key never enters the transcript:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --set-key nvidia

Or store it in the macOS Keychain instead (encrypted at rest, nothing in plaintext):

    security add-generic-password -a "$USER" -s nvidia-api-key -w

Warn them the Keychain prompt is **invisible** - no dots, no echo. A paste that silently fails
creates an empty item, which looks identical to success.

**2. Verify the key resolves.** `--diagnose-key nvidia` reports length and a SHA-256
fingerprint, never key material. Safe to share.

**3. Discover models that actually work.** The catalog over-reports: `--list-models` returns
entries the gateway does not serve, which fail with `404 Function not found`. Probing is the
only way to know. Never assume a listed model works.

**4. Choose up to 5 panelists.** Two rules: prefer **five different vendors** over five strong
models from two - correlated panelists share blind spots and waste money - and keep the roles
distinct. Run `--list-roles` and let the user choose. Then write the panel:

    echo '{"panelists":[...]}' | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --configure

It validates providers, roles, duplicate names, and entry types before writing anything.

**5. Confirm.** `--check` costs ~8 tokens per panelist. Read failures precisely:
`403` = model fine, key wrong. `404`/`410` = key fine, model retired or not served.
