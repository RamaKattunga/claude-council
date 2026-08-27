# Changelog

## 0.2.0

Three design flaws found by reviewers on Reddit within hours of publishing. All three were
real, and one was in the shipped default panel.

### Nothing inspected the prompt before fan-out

The diff went from file to payload verbatim, to every configured provider. Meanwhile the
README documented key handling in detail — fingerprints, mode 0600, Keychain — which implied
a care the data path did not have.

Dispatch now fails closed on credential-shaped material: private keys, AWS / GitHub / OpenAI
/ NVIDIA / Slack tokens, JWTs, bearer tokens, connection strings with inline passwords,
assigned secrets, Luhn-checked card numbers, and US SSNs. Reports line number and a redacted
excerpt, never echoes what it caught. `--allow-secrets` overrides.

Known limits are documented rather than papered over: base64-wrapped secrets, secrets split
across lines, and high-entropy strings with no prefix all get through. Line-by-line literal
matching cannot see them.

### Pattern matching cannot see proprietary data

A confidential algorithm or an unreleased strategy reads as ordinary code. Rather than grow
a worse detector inside a review tool, the pre-dispatch check is now a seam:

```json
"policy_hook": { "command": ["/usr/local/bin/your-guard"], "timeout": 60 }
```

stdin receives the exact bytes that would be dispatched; exit 0 allows, non-zero refuses and
stdout becomes the reason. Any DLP, guard, or in-house script satisfying that works. Fails
closed — a configured backend that is missing, hanging, or erroring blocks dispatch.

### Correlated panelists were counted as independent votes

The value of a panel is decorrelated blind spots, not headcount. Two models sharing a base
miss the same bug the same way and then agree, and naive majority logic promotes it.

Observed here: `gpt-oss` and `nemotron` independently asserted that `except Exception`
catches `KeyboardInterrupt`. It does not. Two votes, one error.

The shipped default panel had the flaw: `gpt-5.2` and `openai/gpt-oss-120b` are both OpenAI
lineage, so six seats were five perspectives. `--show` and every review now print a
CORRELATED PANELISTS block, and consensus means "2+ panelists of different lineage".

### Also

- Test suite no longer makes live API calls. It cleared the key env vars but resolution fell
  through to the Keychain, so on a machine with a real key the tests were billable. 123s → 3.2s.
- 30 tests, up from 13.

## 0.1.0

Initial release. Cross-vendor review over any OpenAI-compatible endpoint, nine review roles,
local configuration UI, reliability tracking with swap suggestions.
