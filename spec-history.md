# SPEC history: live traffic dashboard for `proxy.py`

From `git log -p SPEC.md`: 7 commits, 3–4 Nov 2026. Where each reason comes from:
**[msg]** the commit message, **[diff]** the commit's own changes, **[S1]** Claude Code
session `eab7cb48` (3 Nov, home build), **[S2]** session `9f3bce2d` (4 Nov, improvements).

| # | Commit | What changed in SPEC.md | Why | Source |
|---|---|---|---|---|
| 1 | `f642c65` 3 Nov 17:41 · Home build | First spec (171 lines), committed with the first `proxy.py` + `dashboard.html`. Proxy serves the page and pushes events over SSE; JSONL log; Messages API only; flat newest-first cards; a label and color per kind (prompt / reply / call / result); tool call ↔ result pairing; acceptance checks. Open questions: sessions, diff view, cost. | Message gives no reason. The agent wrote the spec after asking four questions (data path, persistence, API formats, layout); then "go ahead and build it". | [S1] 15:09–15:17 |
| 2 | `6c06abc` 3 Nov 17:53 · Spec matches the build | Rewritten (+371 / −105): split into `proxy.py` (CLI, routes, relay, redaction, stream assembler, hub, events) and `dashboard.html` (exact colors, blocks, reply, live behavior), plus a new "Decided during the home build" list. | Message gives no reason. Asked whether it had asked before deciding, the agent listed 22 decisions it made itself; 16–22 were made mid-build without approval (e.g. "New this turn" = after the last assistant message, "changed" badge per model, page reloads on proxy restart, favicon 404). Instruction: keep as built, and make the spec complete enough that a fresh build reproduces it. | [S1] 15:50–15:51, [diff] |
| 3 | `1f5eab2` 4 Nov 10:31 · **⇢ SWITCH TO EDITING THE CODE** + sessions | New "Sessions" section: session id from `metadata.user_id`, chips with title / count / last activity, session colors, filter; the "changed" badge only compares within a session. | **Switch:** the home build met the spec and the remaining improvements were small additions, so a full rebuild per change costs more than it teaches; from here the code is edited and SPEC.md is updated in the same commit. **Change:** two conversations were mixed in one list; the title is the most common opening prompt, because every session starts with a one-off `quota` request. | [msg], [S2] |
| 4 | `94ed9d5` 4 Nov 10:46 · Cost and context | New "Cost and context" section: a price table (live pricing page, 4 Nov), a cost formula per token type, a context bar, running totals per session. | Open question from #1. `usage` splits cache writes into 5 min / 1 h and reports `speed` and `inference_geo`, so the bill can be priced exactly. | [msg] |
| 5 | `cec1e24` 4 Nov 10:52 · Diff view | "Thread" (session + model + first message) replaces "same model" for the badge; diff view rules; acceptance check 4 updated; Open questions now empty. | Comparing per session still gave false badges: side calls to the *same* model (#34) flipped it twice. With threads, one real change remains (#33 gains `WebFetch`). | [msg] |
| 6 | `47a8229` 4 Nov 11:13 · Usage Meter (partner's branch) | New "Usage Meter" section: two new files, the `anthropic-ratelimit-unified-*` headers, the 5h / 7d row in the top bar, its own acceptance checks. | Show subscription usage and reset times; built as an add-on, so existing files only gain the lines that load it. | [msg] |
| 7 | `08a41e7` 4 Nov 11:15 · Merge | SPEC conflict resolved: "None right now." under Open questions, then the Usage Meter section. Fast-forwarded into `main`. | Bring #5 into the partner's branch before merging. | [msg], [S2] |

**Switch point:** commits 1–2 come from a full build from the spec; from commit 3 on, the code was edited.

## Findings in the traffic

From the commit messages above and the analysis of `traffic.jsonl` in [S2] (all replies
from `claude-opus-5-5`):

- **Sessions are in the request.** `metadata.user_id` is a JSON string containing
  `session_id`; `count_tokens` and `HEAD /api/hello` requests carry none.
- **Hidden side calls.** Each session opens with a one-off `quota` request to the main
  model, and Claude Code makes further calls to the same model with a tiny system prompt
  and no tools (#34). Neither is part of the conversation you see.
- **The system prompt starts with a billing line**, `x-anthropic-billing-header:
  cc_version=…; cc_entrypoint=cli;`, so it changes with the Claude Code version.
- **The tool list grows mid-conversation:** deferred tools are added when loaded through
  `ToolSearch` (#33 gained `WebFetch`). `cache_control` markers move between requests
  even when nothing else changes.
- **Caching dominates the cost.** Over 25 replies, ≈ $3.42 at API prices: 1-hour cache
  writes 43 %, output 29 %, cache reads 27 %, uncached input 1 %. The largest prompt was
  166k tokens, 17 % of the 1M window.
- **Every reply reports subscription usage** in undocumented rate-limit headers (e.g.
  5h 4 %, 7d 1 %, binding window `five_hour`; `overage-status: rejected` while `status:
  allowed`).
