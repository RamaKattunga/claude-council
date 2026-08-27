---
description: Open the local web UI to configure keys, models, panelists and roles
---

Launch the configuration UI:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/council.py --serve
```

Run it in the **background** or tell the user to run it in a terminal they will leave open — the
server runs in the foreground and stops when that terminal closes, when interrupted, or after 30
minutes idle.

It prints a URL containing a one-time token and opens the browser automatically:

```
Council configuration UI: http://127.0.0.1:8787/?t=<token>
```

The token is **required** and changes every run. Browsing to bare `127.0.0.1:8787` loads the page
but every action fails. If the user reports "failed to fetch", the server has stopped — restart it.
If they get a stale-token banner, the server restarted since the page loaded; give them the new URL.

Add `--port N` if 8787 is taken.

What the page does: store API keys (keys go in, only SHA-256 fingerprints come back), probe the
catalog for models that actually respond, pick panelists and assign roles, and show reliability
history.
