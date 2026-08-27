---
description: Run a cross-vendor council review on a diff, file, or design question
---

Run a council review on: $ARGUMENTS

(If no target given, default to the working diff: `git diff` then `git diff --staged`.)

**1. Scope it.** The panel is not a whole-codebase tool. Reviews need a focused diff or a single
module. Large prompts push slow panelists past their timeout and can trip gateway 404s.

**1b. Scope it for disclosure, not just for size.** The diff is sent in full to every
panelist — N providers, N retention policies, N jurisdictions, and no way to un-send it. The
runner refuses prompts containing anything credential-shaped, but that is a backstop, not a
boundary: it will not recognise proprietary algorithms, customer data, or unreleased
strategy. If the user has not signalled that this code can go to third-party inference
providers, ask before dispatching.

**2. Build the prompt as a file** - never inline a diff, quoting will bite you. Include what the
code is for, and demand concrete findings:

    mkdir -p /tmp/council && git diff main...HEAD > /tmp/council/diff.txt
    {
      echo "Review this diff. Context: <what it does, what the system is>."
      echo "Report ONLY real defects. For each: file:line, concrete failure scenario, fix."
      echo "If you find nothing real in your area, say so plainly. At most 5 findings."
      echo; echo '```diff'; cat /tmp/council/diff.txt; echo '```'
    } > /tmp/council/prompt.txt

**3. Fan out.** All panelists run in parallel; one dead endpoint does not kill the run.

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --prompt-file /tmp/council/prompt.txt

**4. VERIFY EVERY FINDING against the real file before relaying it.** Panelists hallucinate line
numbers, invent APIs, and re-report issues already fixed. Expect roughly 1 stale or wrong finding
in 15. This step is not optional - passing on an unverified finding is worse than no review.

**5. Synthesize.** Never dump raw panelist output at the user. Produce:

- **Consensus** - flagged by 2+ panelists. Lead with it.
- **Divergence** - one panelist only. Say who, and why it may be a false positive.
- **Contradiction** - panelists disagree. State it; do not paper over it.
- **My read** - where I agree and where I don't, with reasoning. I am not a vote counter.

Majority agreement is evidence, not proof - models trained on similar data are wrong together.

**6. Mention health** if `--health` flags anything after the run.
