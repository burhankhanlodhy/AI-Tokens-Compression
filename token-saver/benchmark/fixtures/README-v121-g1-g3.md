V1.2.1 G1/G3 benchmark corpus inventory

Corpora are deterministic outputs of `python3 benchmark/gen_v121_g1_g3_corpora.py`.
Each JSON file is SHA-256 pinned beside it; `run_e2e_v121_ab.py --corpus PATH --k K`
verifies that pin before sending the same scenarios through baseline and treatment.
The runner retains upstream-reported prompt-token counts and ratio-of-sums per
segment. Tool and result gates are evaluated on whole-prompt as-shipped reduction;
the treatment ledger's isolated `tool_compression_saved_total` is emitted beside
those numbers. The runner also reports that isolated total's share of baseline
upstream prompt tokens. Both results carry their scenario population and corpus
SHA-256; the isolated proxy estimate is not upstream usage. No content was selected
against a target savings percentage.

G1: `v121_g1_tool_heavy.json` — three CRM/MCP contact-search scenarios with 24,
32, and 40 tools each. Each tool has pretty-printed JSON-schema parameter blocks,
verbose operational descriptions, and email descriptions drawn from the current
explicit allow-list (`Email address`, `User email address`, `The user email
address`). Additional long descriptions remain outside that allow-list. Segment:
`tool`; applicable whole-prompt gate: >=8% vs v1.2.0.

G3: `v121_g3_result_heavy.json` — five tool-result scenarios:

- `v121-g3-ls-noisy-24000`: `ls -la` listing with temporary-artifact noise; 24k-char class; over-cap file-listing filter case.
- `v121-g3-json-api-42000`: verbose, pretty-printed CRM JSON response; 42k-char class.
- `v121-g3-repeated-listing-24000`: byte-identical tool-result request payload to the first listing scenario; cache-path repeat.
- `v121-g3-repeated-logs-70000`: 70k-char log dump with repeated traceback frames.
- `v121-g3-boundary-ls-15000`: noisy listing near the configured 5,000-token cap; approximately 15.1k chars and 5,048 tokens under the fixture tokenizer (`gpt-4o-mini`).

The over-cap examples use 24k–70k chars and each exceeds 5,000 tokens under the
fixture tokenizer. The at-cap boundary is measured at 4,900–5,100 tokens; all
scenario text stays within the specified 5k–80k-char range. The result floor remains
>=25% on `result` as-shipped whole-prompt reduction.
