# LLM_PORTABILITY.md

The LLM narrates; it never decides. Everything the product does is computed deterministically before
any model is called, and the product is fully usable with the LLM switched off. That is what makes
the provider swappable — nothing depends on a specific model's behaviour.

## The interface

One method. Deliberately.

```python
# services/detector/llm/provider.py
class LLMProvider(Protocol):
    def complete(self, prompt: str, system: str, *, max_tokens: int = 800,
                 temperature: float = 0.2) -> str: ...
    def health(self) -> bool: ...
```

Implementations live in `services/detector/llm/providers/`. `OllamaProvider` is the default
(`qwen2.5:7b-instruct`, ~5 GB VRAM alongside the detector's own models on the RTX 5060).
`AnthropicProvider`, `OpenAIProvider` and `VLLMProvider` are drop-ins; adding one must not touch a
single caller.

Selection is configuration, never code:

```yaml
# policy/llm.yaml
provider: ollama            # ollama | anthropic | openai | vllm | null
model: qwen2.5:7b-instruct
base_url: http://localhost:11434
timeout_seconds: 30
cache: true
```

`provider: null` is a supported, tested configuration — the `NullProvider` fails `health()` and
every call falls back to the deterministic template. **CI runs the test suite with `provider: null`**
so the fallback path can never rot.

## Rules that must hold for any provider

1. **Never on the batch path.** Scoring 1M employees makes zero LLM calls. The narrator runs only
   when a reviewer opens an alert or asks a question, and results are cached per alert.
2. **The model only rephrases the evidence bundle.** It never computes, never infers a figure, never
   consults anything outside the bundle it was handed.
3. **Numeral grounding is enforced in code, not asked for in the prompt.** Every numeric token in
   the output must appear in the input bundle. A failed check discards the output and returns the
   deterministic template. Prompt instructions are a hint; the post-check is the guarantee.
4. **Graceful degradation is the default state, not an error path.** `source: "template"` is a
   normal API response and the UI renders it without degraded-mode styling.
5. **No provider-specific formatting in prompts.** No XML tags that only one family respects, no
   JSON-mode assumptions, no function calling. Prompts are plain text with a plain-text expectation,
   because that is the only thing every backend does identically.
6. **Temperature ≤ 0.3.** This is narration of fixed facts, not composition.

## Prompts are files, never inline strings

```
services/detector/prompts/
  explain_alert.v1.txt      # evidence bundle -> one plain-English paragraph
  suggest_actions.v1.txt    # expand recommended_actions with context
  ask_about_alert.v1.txt    # grounded Q&A, explicit "I don't know" instruction
```

Versioned by filename. Changing a prompt means adding `.v2.txt` and switching the reference in
`policy/llm.yaml`, so a regression is a one-line revert and the old behaviour stays reproducible.

Every prompt states, in its own words: use only the figures in the bundle; if the answer is not in
the bundle, say you do not know; write for a non-technical HR reviewer; no jargon.

## What must not depend on a model

| Concern | Where it actually lives |
|---|---|
| Whether something is an anomaly | `policy/rules/*.yaml`, layers 1–3 |
| The severity and score | `policy/fusion.yaml`, layer 4 |
| The financial impact figure | Computed in layer 4 from the rule's `financial_impact` expression |
| The recommended actions | `policy/rules/*.yaml` — the LLM may only expand them with context |
| The peer comparison | Layer 2, computed from the cohort |
| Anything written to Postgres as fact | The batch. Narration is cached text, never a stored fact. |

If removing the LLM entirely would change any number in the system, something has been built wrong.

## Testing the swap

`services/detector/tests/test_llm_portability.py` runs the same three prompts against every
configured provider plus `NullProvider`, and asserts:

- the grounding post-check passes on well-formed output and **rejects** a deliberately
  hallucinated figure;
- the template fallback produces a complete, readable explanation with no provider available;
- the cache returns the identical string on a second call without a second provider call;
- no response contains any term from the banned-jargon list (`docs/DESIGN_SYSTEM.md`).
