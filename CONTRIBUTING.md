# Contributing

## Ground rules

**No dependencies.** `scripts/council.py` is standard library only, Python 3.9+. This is a hard
constraint, not a preference — it means the plugin installs and runs with zero setup on any Mac or
Linux box. A PR adding `requests` will be declined no matter how much cleaner it reads.

**Never log, print, or return key material.** Diagnostics report length and a SHA-256 fingerprint.
The web UI accepts keys and returns fingerprints. If your change makes a key observable anywhere —
stdout, a log, an HTTP response, an error message — it will not be merged.

**Keep `validate_panelists` pure.** It returns problems; it writes nothing and never calls
`sys.exit`. It runs inside HTTP handler threads, where an exit kills the connection instead of
returning an error. There is a test for this contract because a previous refactor broke it and
all ten existing tests still passed.

## Running tests

```bash
python3 -m unittest discover -s tests -v
```

Tests are fully offline: no network, no API keys, no real Keychain access. Keep them that way — a
test suite that needs credentials is a test suite nobody runs.

## Adding a provider

Any OpenAI-compatible endpoint should work already. Add an entry under `providers` in
`config.json` with `base_url` and `api_key_env`, and optionally `keychain_service`.

If a gateway needs special handling, put it in `_post_chat`, which already adapts to parameter
quirks by reading the provider's own structured error. Prefer adapting automatically over asking
users to hand-tune config for a quirk the provider describes precisely.

## Adding a role

Roles live in `defaults/roles.json`. A good lens:

- names concrete defect classes, not vague qualities ("N+1 queries, unbounded memory growth", not
  "look for performance issues")
- demands a specific failure scenario, not a severity label
- says explicitly to report nothing rather than invent findings
- is genuinely different from the other eight — overlapping roles waste money and converge on the
  same shallow findings

## Reporting model behaviour

Which models give useful review is the least documented thing here, and the most valuable. If you
seat a model and it pads, hallucinates, or consistently outperforms — open an issue with the model
ID, the role, and an example. Latency numbers from `--health` help too.
