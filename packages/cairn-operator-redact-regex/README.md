# cairn-operator-redact-regex

Zero-dependency regex PII/secret redactor — the **builtin default** provider
for the substrate `redact.text` service (ADR-0028). Detection lives here, in
the operator pack; core holds only the `Redactor` interface + the LLM-gateway
chokepoint (`cairn/core/obs/redact.py`).

Recognizers (stdlib `re`): private-key (PEM) · JWT · AWS access key · OpenAI
`sk-…` · GitHub `ghp_…` · email · credit card (Luhn-checked) · IPv4. Matches are
replaced with `[REDACTED:<kind>]`.

**Optional (ADR-0028).** Redaction is off unless a profile binds it:

```toml
# in a secure / prod profile
[profile.bindings]
"redact.text" = "redact/text"
```

`local-dev.toml` leaves it unbound (dev data is usually synthetic). Richer
detection ships later as `operator-redact-presidio` (MIT) — swap the binding,
no core change.
