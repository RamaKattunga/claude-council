# claude-council

Cross-vendor code review for Claude Code. Sends your diff to several models **in parallel**, each
with a different review role, then synthesizes consensus and disagreement.

The point is **bias-breaking**. Every Claude subagent shares Claude's priors, so a Claude-only
review cannot catch what Claude systematically misses. A panel from different vendors can.

## Why not the existing council plugins

Other council plugins invoke **Codex CLI and Gemini CLI as subprocesses**. They expose no
`base_url`, so they cannot reach OpenAI-compatible endpoints — which rules out NVIDIA NIM,
Together, OpenRouter, Zhipu, or a local vLLM.

This one speaks plain HTTPS to any OpenAI-compatible endpoint. Because the models never touch
your filesystem, none of the sandboxing those plugins need applies here.

## Install

```
/plugin marketplace add RamaKattunga/claude-council
/plugin install claude-council@claude-council-marketplace
```

Then `/council:setup`.

Requires Python 3.9+. **No dependencies** — standard library only, no venv, no pip.

## Commands

| Command | Does |
|---|---|
| `/council:setup` | Store a key, discover working models, choose panelists and roles |
| `/council:review [target]` | Run a review and synthesize the results |
| `/council:health` | Reliability report, with swap recommendations for weak panelists |

## Configuration UI

```bash
python3 scripts/council.py --serve
```

Opens a local page for key entry, live model probing, panelist and role selection, and a health
view. Everything the CLI does, without hand-writing JSON.

Security posture, since this opens a socket: bound to `127.0.0.1` only (never `0.0.0.0`), a random
token is required on every API call and changes each run, the `Host` header is validated to defeat
DNS rebinding, the page declares a CSP and loads no external resources, and the server stops after
30 minutes idle. Keys can be submitted but are never sent back — only fingerprints leave the
server.

## Keys

Resolution order: **environment variable → macOS Keychain → `credentials.json` (0600)**.

Keys are never written to `config.json`, never passed as command-line arguments (so they stay out
of `ps` and shell history), and never printed. `--diagnose-key` reports length and a SHA-256
fingerprint only.

Keychain is the best option — encrypted at rest, nothing in plaintext, nothing in your backups:

```bash
security add-generic-password -a "$USER" -s nvidia-api-key -w
```

The prompt is invisible: no dots, no echo. A paste that silently fails creates an **empty** item
that looks exactly like success. Verify with `--diagnose-key nvidia`.

One NVIDIA key is account-level and works for every model — you do not need one per model.

## Roles

Nine ship by default: correctness, architecture, security, regression, performance, simplicity,
data-integrity, product, generalist.

**Distinct roles are the whole design.** Five models given the same generic "review this" prompt
converge on the same shallow findings. Five models given five different questions find five
different classes of defect. Edit `~/.claude/council/roles.json` to customize; the bundled
defaults are used until you create one.

## Health and model swapping

Reliability is recorded after every run, at no extra cost. `--health` flags two independent
failure modes:

- **UNRELIABLE** — below 80% success over 3+ runs
- **TOO SLOW** — averaging over 120s. A panelist that always answers but takes minutes is
  unusable, and the panel is only as fast as its slowest member

`--suggest-swap <panelist>` probes up to 16 candidates in parallel, ranks by measured latency,
prefers vendors not already seated, and prints the exact command to apply. It never edits your
config — you run the command.

Check the **cause** before swapping. A slow panelist running `reasoning_effort: high` is usually a
bad setting, not a bad model.

## Known gotchas

- **`/v1/models` over-reports.** NVIDIA's catalog lists models the gateway does not serve; they
  fail with `404 Function not found`. Probing is the only way to know. Dead models are cached in
  `health.json` and skipped afterwards.
- **Panelists hallucinate.** Expect roughly 1 stale or wrong finding in 15. Always verify against
  the real file before acting. Majority agreement is evidence, not proof — models trained on
  similar data are wrong together.
- **Scope your reviews.** A focused diff, not a whole codebase. Large prompts push slow panelists
  past their timeout.
- **529 is transient**, not your config.

## Layout

Plugin code and user data are separate, so upgrading never touches your keys.

```
claude-council/              ~/.claude/council/     (user data, not in the repo)
├── scripts/council.py       ├── config.json
├── defaults/roles.json      ├── roles.json         (optional override)
├── commands/                ├── credentials.json   (0600, only if not using Keychain)
├── skills/council/          └── health.json
└── tests/
```

Override the data location with `COUNCIL_HOME`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

10 tests, fully offline — no network, no keys, no Keychain access.

## License

MIT
