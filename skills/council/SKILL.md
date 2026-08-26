---
name: council
description: Get independent second opinions on code, a diff, or a design decision from non-Claude models (OpenAI plus DeepSeek, Kimi, Nemotron, MiniMax via NVIDIA NIM), each running a distinct review role, then synthesize consensus and disagreement. Use when the user says "ask the council", "second opinion", "what do other models think", or before merging high-stakes code (payments, auth, tenant isolation, trading logic, migrations). Also handles council setup and reconfiguration.
---

# Council

Cross-vendor review. The point is **bias-breaking**: every Claude subagent shares my priors,
so a Claude-only review cannot catch what Claude systematically misses. These panelists can.

Runner: `${CLAUDE_PLUGIN_ROOT}/scripts/council.py` (stdlib only, no venv).
Config: `~/.claude/council/config.json` (user data, outside the plugin) · Roles: `~/.claude/council/roles.json`

## When to use

Worth the latency and tokens for payments, billing, auth, tenant isolation, trading logic,
schema migrations, and design decisions that are expensive to reverse.

Not worth it for typo fixes, formatting, or exploratory scripts.
For Claude-only multi-agent review use `/code-review` — faster and free.

## Setup (first run, or when the user wants to change models)

**1. Get the key.** NVIDIA issues keys only through the web UI — there is no key-mint API,
so this step cannot be fully automated. Send the user to <https://build.nvidia.com>, any model
page, "Get API Key". One `nvapi-...` key is **account-level and works for every model** — they
do not need one per model. If they want, drive their already-logged-in Chrome to that page;
never ask for or handle their password.

**2. Store it.** Reads stdin, so the key never lands in argv, `ps` output, or shell history:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --set-key nvidia    # then paste
```

Stored in `credentials.json` at mode 0600. An env var of the same name wins if set.

**3. Discover real models.** Always do this — providers retire models constantly and a stale
ID fails with a confusing 410:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --list-models --provider nvidia
```

**4. Pick up to 5 and assign roles.** Present the candidates and let the user choose. Two rules:
prefer **five different vendors** over five strong models from two (correlated panelists waste
money), and keep the roles distinct.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --list-roles
```

Write the panel by piping a spec to `--configure` (it validates providers, roles, and duplicate
names before writing anything):

```bash
echo '{"panelists":[
  {"name":"kimi","provider":"nvidia","model":"moonshotai/kimi-k3","role":"security","enabled":true}
]}' | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --configure
```

**5. Verify.** `--check` spends ~8 tokens per panelist:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --check
```

Read the failures precisely — they distinguish the two failure modes:
`403 Authorization failed` = model ID is fine, key is wrong.
`404` or `410 Gone` = the key is fine, the **model ID** is wrong or retired.

## Running a review

**1. Build the prompt as a file.** Never inline a diff — quoting will bite you.

```bash
mkdir -p /tmp/council && git diff main...HEAD > /tmp/council/diff.txt
{
  echo "Review this diff. Context: <what the change does, what the system is>."
  echo "Report only real defects. For each: file:line, concrete failure scenario, fix."
  echo; echo '```diff'; cat /tmp/council/diff.txt; echo '```'
} > /tmp/council/prompt.txt
```

**2. Fan out.** All panelists run in parallel; one dead endpoint does not kill the run.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --prompt-file /tmp/council/prompt.txt
```

Subset: `--only kimi,deepseek`

**3. Synthesize — do not dump raw panelist output at the user.** Produce:

- **Consensus** — flagged by 2+ panelists. Highest confidence. Lead with it.
- **Divergence** — one panelist only. Say who, and why it may be a false positive.
- **Contradiction** — panelists disagree. State it explicitly; do not paper over it.
- **My read** — where I agree and where I don't, with reasoning. I am not a vote counter.

**Then verify every claim against the actual file before relaying it.** Panelists hallucinate
line numbers and invent APIs. A confidently-wrong finding from a second vendor is still
confidently wrong. Majority agreement is evidence, not proof — models trained on similar data
share blind spots and can be wrong together.

## Roles

Nine predefined in `roles.json`: correctness, architecture, security, regression, performance,
simplicity, data-integrity, product, generalist. A panelist picks one via `"role"`, or overrides
with an inline `"lens"` for a one-off.

**Distinct roles are the whole design.** Five models given the same generic "review this" prompt
converge on the same shallow findings. Five models given five different questions find five
classes of defect. Preserve that when editing.

To change a role's wording for every panelist using it, edit `roles.json` — not the panelists.

## Reference

```bash
council.py --show           # current panel and which keys resolve
council.py --list-roles     # available roles
council.py --list-models    # authoritative model IDs (works without a key on NVIDIA)
```

Per-panelist tuning in `config.json`: `temperature`, `top_p`, `max_tokens`, and `extra_body`
(passed through verbatim — this is how DeepSeek's `chat_template_kwargs.thinking` and
`reasoning_effort` are set). Responses that arrive only in `reasoning_content` are handled.

Any OpenAI-compatible endpoint works: add an entry under `providers` with a `base_url` and an
`api_key_env`. GLM is **not** available on NVIDIA NIM (verified against the live catalog) — to
use it, add Zhipu as its own provider.
