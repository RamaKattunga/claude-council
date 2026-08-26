---
description: Show council reliability and recommend replacing weak models
---

Report panelist reliability and act on it.

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --health

Health is recorded automatically after every run, so this costs nothing.

Two independent failure modes are flagged - a panelist can be broken in either way:

- **UNRELIABLE** - success rate below 80% over 3+ runs
- **TOO SLOW** - average above 120s. A panelist that always answers but takes minutes is
  unusable, and the panel is only as fast as its slowest member.

If something is flagged, **proactively offer the swap** - do not wait to be asked:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --suggest-swap <panelist>

That probes up to 16 candidates in parallel (~10 tokens each), ranks by measured latency, prefers
vendors not already seated, records models the gateway does not serve so they are skipped later,
and prints the exact `--configure` command. It never changes config on its own - the user runs it.

**Read the cause before recommending a swap:**

| Cause | Do this first |
|---|---|
| `slow` + the panelist sets `extra_body` reasoning options | Lower `reasoning_effort` before replacing the model - the model may be fine, the settings are not |
| `timeout` | Raise its per-panelist `"timeout"` |
| `overloaded` (529) | Provider capacity. Transient - may recover on its own |
| `retired` (410) | Gone. Must be replaced |
| `auth` (401/403) | Key problem, not the model. Run `--diagnose-key` |

Swapping a good model because of a bad setting is the common mistake. Check the cause first.
