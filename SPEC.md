# SPEC: live traffic dashboard for proxy.py

## Goal

A browser page that shows, live, every request an agent (Claude Code) sends through
`proxy.py` and every reply it gets back. The main goal is **readability**: at a glance
you should be able to tell apart **your prompt**, **the model's reply**, **a tool call**
and **that tool's result**.

## Non-goals

- Changing traffic. The proxy keeps forwarding requests and replies untouched.
- Parsing other API formats. Only the Anthropic Messages API is understood; anything
  else is shown as pretty-printed JSON or plain text.
- Accounts, multiple users, remote access. The proxy listens on 127.0.0.1 only.
- Grouping requests into sessions (possible later; see Open questions).

## Decisions

| Topic | Decision |
|---|---|
| Data path | `proxy.py` itself serves the page and pushes events to it with server-sent events (SSE). One process, one port. |
| Dependencies | Python standard library only. The page is one file, `dashboard.html`, next to `proxy.py`: plain JS and CSS, no build step, no CDN, no external fonts. |
| Persistence | Every finished exchange is appended to a JSONL log on disk. History survives page reloads and proxy restarts. |
| Format | Anthropic Messages API (`POST /v1/messages`, streaming and non-streaming). |
| Layout | Flat list, newest first, one card per request. Repeated history is collapsed. |

## Files

| File | Role |
|---|---|
| `proxy.py` | The proxy, the event hub, the log writer and the dashboard server. |
| `dashboard.html` | The page. Read from disk on every request to `/__dashboard`, so edits show on reload. |
| `traffic.jsonl` | The log (default location, next to `proxy.py`). |
| `.gitignore` | Contains `traffic.jsonl`, because the log holds your prompts and file contents. |

## proxy.py

### Command line

```
python3 proxy.py [--port 8041] [--upstream https://api.anthropic.com]
                 [--log PATH] [--no-log]
```

- `--port`: default 8041. The server binds to `127.0.0.1` only.
- `--upstream`: where to forward; trailing `/` stripped. Default `https://api.anthropic.com`.
- `--log PATH`: default `traffic.jsonl` in the same folder as `proxy.py`.
- `--no-log`: keep history in memory only (nothing read or written).

On start it prints three lines:

```
proxy listening on http://localhost:<port>  ->  <upstream>
run the agent with:  ANTHROPIC_BASE_URL=http://localhost:<port> claude
dashboard:           http://localhost:<port>/__dashboard
```

### Routes

```
claude ──► proxy.py :8041 ──► upstream (api.anthropic.com)
              │  (relay, unchanged)
              ├─► appends to traffic.jsonl
              └─► broadcasts to SSE subscribers
browser ◄── GET /__dashboard        (the HTML page)
        ◄── GET /__history          (recent + in-flight exchanges, JSON)
        ◄── GET /__events           (SSE stream)
```

- Any `GET` whose path starts with `/__`, and `GET /favicon.ico`, is handled locally and
  **never forwarded**. Unknown `/__` paths and `/favicon.ico` get `404 not found`
  (so the browser's favicon request never reaches the API).
- `/__dashboard`: `200 text/html` with the file's bytes; `500` with the text
  "dashboard.html is missing next to proxy.py" if it can't be read.
- `/__history`: `200 application/json`, `{seq, exchanges}` (see Hub).
- Local responses carry `Content-Length` and `Cache-Control: no-store`.
- Every other request (`POST`, `HEAD`, other `GET`s) is relayed.

### Relay

Unchanged from the original proxy except for the hub calls:

1. Read the body (by `content-length`), note the start time, and register the exchange
   with the hub (`begin`) as `{method, path, headers: redacted, body: parsed}`.
2. Print the request to the terminal: `====…` line, `[HH:MM:SS] >>> METHOD path`,
   each header (redacted), then the body pretty-printed as JSON, or as text if it isn't JSON.
3. Connect upstream with a 10 s connect timeout, then set a 600 s read timeout. Forward the
   same method and `upstream path + request path`, with all headers except the hop
   headers in `SKIP` (`host, connection, keep-alive, transfer-encoding, te, trailers,
   upgrade, proxy-authorization, proxy-authenticate, accept-encoding, content-length`).
   Dropping `accept-encoding` keeps replies uncompressed and readable.
4. If connecting, sending or getting the response raises `OSError`: print
   `!!! cannot reach <upstream>: <err>`, finish the exchange with
   `{error: "cannot reach <upstream>: <err>", duration_ms}`, answer `502`, close.
5. Print `[HH:MM:SS] <<< status reason`, send the status and the upstream headers except
   `SKIP` and `content-encoding`, add `Connection: close`.
6. `HEAD`: finish with `{status, reason, headers, body: null, duration_ms}`.
7. Otherwise pass the reply through as it arrives, using `read1(4096)` so each chunk is
   forwarded immediately and flushed. Each chunk is decoded with an incremental UTF-8
   decoder (errors replaced, so a character split across chunks prints correctly),
   written to stdout, and:
   - if the reply's `content-type` contains `text/event-stream`: fed to a
     `StreamAssembler` and sent to the hub as a `chunk`;
   - otherwise collected and parsed as the body at the end.
   If writing to the agent or reading upstream raises `OSError`, the loop stops and the
   response gets `error: "stream interrupted: <err>"`.
8. Print `[HH:MM:SS] --- done`, then finish the exchange with
   `{status, reason, headers: redacted, body, duration_ms, error?}`.

`body` parsing everywhere: empty → `null`; valid JSON → the parsed value; otherwise the
text decoded as UTF-8 with replacement characters.

### Redaction

Header values of `authorization` and `x-api-key` (any case) are replaced by their first
16 characters plus `...`. This applies to terminal output, events, `/__history` and the
log; the full value is never stored. Response headers go through the same function.

### StreamAssembler

Rebuilds the final message from the Anthropic SSE stream. It is fed decoded text, splits
it into lines (keeping an unfinished last line for the next chunk, stripping `\r`),
collects `data:` lines (one leading space removed), and handles one event per blank
line. Each event's JSON is applied as follows; any malformed event is ignored and must
never break the relay:

| Event | Effect |
|---|---|
| `message_start` | message = copy of `event.message`, with `content` as a list |
| `error` | message (created as `{content: []}` if missing) gets `error = event.error` |
| (anything else before `message_start`) | ignored |
| `content_block_start` | pad `content` up to `index`, store a copy of `content_block`; if it has an `input` key, start an empty partial-JSON buffer for that index |
| `content_block_delta` | `text_delta` → append to `text`; `input_json_delta` → append to that index's buffer; `thinking_delta` → append to `thinking`; `signature_delta` → set `signature`; `citations_delta` → append to `citations` |
| `content_block_stop` | parse the buffer as the block's `input` (empty → `{}`; invalid JSON → keep the raw string) |
| `message_delta` | merge `delta` into the message (e.g. `stop_reason`), merge `usage` into `message.usage` |

`result()` closes any blocks still open and returns the message (or `null` if the stream
never started).

### Hub

Shared state guarded by **one lock**, so a history snapshot and its event sequence number
always agree:

- `next_id`: exchange ids are increasing integers. On start, the log is read and
  `next_id` = highest logged id + 1, so ids keep counting across restarts. Unparsable log
  lines are skipped.
- `finished`: the last 200 finished exchanges (loaded from the log on start).
- `inflight`: exchanges whose reply hasn't finished, each with the stream text so far.
- `seq` and `recent`: every event gets the next `seq` number (starting from 0 at each
  proxy start) and is kept in a buffer of the last 5000 events for reconnect catch-up.
- `clients`: one queue per connected page.

Operations:

- `begin(request)` → new id; records `{id, time (local ISO, seconds), request,
  response: null}`; emits `request`.
- `chunk(id, text)` → appends to the in-flight stream text; emits `chunk`.
- `finish(id, response)` → moves the exchange to `finished`; emits `response`; appends
  `{id, time, request, response}` as one JSON line (UTF-8, not ASCII-escaped) to the log.
- `snapshot()` → `{seq, exchanges}`: finished plus in-flight exchanges sorted by id;
  in-flight ones carry `stream` (the raw stream text so far) and `response: null`.
- `subscribe(since)` → a queue. If `since` is given and is greater than the current
  `seq` (the proxy restarted) or older than the oldest buffered event, a `reset` event is
  queued; otherwise every buffered event with `seq > since` is queued.

### Events (`/__events`)

- `since` comes from the `Last-Event-ID` header if present, else from the `?since=`
  query parameter; non-numeric values count as none.
- Response: `200`, `Content-Type: text/event-stream`, `Cache-Control: no-store`,
  `Connection: close`. Each event is written as
  `id: <seq>\ndata: <JSON, not ASCII-escaped>\n\n`.
- If nothing is sent for 15 s, a `: ping` comment is written to keep the connection open.
- When writing fails (page closed), the queue is unsubscribed.

Event payloads (all in `data`, with a `type` field):

| `type` | When | Other fields |
|---|---|---|
| `request` | request received | `id, time, request: {method, path, headers, body}, response: null` |
| `chunk` | each streamed piece of a reply | `id, data` (the raw SSE text) |
| `response` | reply finished or failed | `id, response: {status, reason, headers, duration_ms, body, error?}` |
| `reset` | the page asked to resume from a point the proxy can't serve | none |

For streamed replies `body` is the assembled message (`content`, `stop_reason`,
`usage`, …), so neither the page nor the log has to parse SSE fragments.

### Log format

One JSON object per line: `{id, time, request: {...}, response: {...}}`, the same shapes
as the events (no `stream` field). Secrets are redacted before writing.

## dashboard.html

### Look

- Fonts: system UI sans-serif at 14px / 1.5 for text; `ui-monospace, SFMono-Regular,
  Menlo, Consolas, monospace` for card headers, labels and JSON.
- Content column: max width 1100px, centered, 16px padding (12px under 600px wide).
- Colors are CSS variables, with a dark set under `prefers-color-scheme: dark`:

| Variable | Light | Dark |
|---|---|---|
| `--bg` / `--card` / `--fg` | `#f5f6f8` / `#ffffff` / `#1d2026` | `#111318` / `#1a1d23` / `#e6e8eb` |
| `--muted` / `--line` / `--code-bg` | `#6b7280` / `#e2e5ea` / `#f3f4f6` | `#9aa1ab` / `#2c3038` / `#22262d` |
| `--you` / `--you-bg` | `#2563eb` / `#eef4ff` | `#6ea8ff` / `#15233a` |
| `--claude` / `--claude-bg` | `#15803d` / `#edf9f1` | `#4ade80` / `#13281b` |
| `--call` / `--call-bg` | `#b45309` / `#fff6e8` | `#fbbf24` / `#2b2311` |
| `--result` / `--result-bg` | `#7c3aed` / `#f4efff` | `#c4a5ff` / `#231a37` |
| `--error` / `--error-bg` | `#dc2626` / `#fdf0f0` | `#f87171` / `#331a1b` |
| `--sys` / `--sys-bg` | `#6b7280` / `#f3f4f6` | `#9aa1ab` / `#20232a` |
| JSON key / string / number / literal | `#9333ea` / `#0f766e` / `#c2410c` / `#2563eb` | `#d8b4fe` / `#5eead4` / `#fdba74` / `#93c5fd` |

- Label tags: bold 11px monospace, small rounded pill, background = the kind's accent;
  text white in light mode, `#111318` in dark mode.

### Top bar (sticky)

Left to right, wrapping on narrow screens:

- Title "Agent Traffic".
- Connection indicator: a dot plus text. "connecting…" (grey) at start, "live" (green)
  when the event stream is open, "reconnecting…" (red) on error, "proxy not reachable,
  retrying…" (red) if `/__history` can't be fetched.
- Filter box ("Filter cards…"): case-insensitive substring search over a card's request
  body, response body and path (searches data, so it also matches collapsed content);
  applied 150 ms after you stop typing.
- Checkbox "Hide non-message requests", **checked by default**. A message request is a
  `POST` whose path matches `^/v1/messages(\?|$)` (so `?beta=true` counts,
  `/v1/messages/count_tokens` does not).
- "Pause" button: while paused, incoming events (except `reset`) are queued, the button
  turns amber and reads "Resume (N)"; clicking it again handles the queued events in order.
- "Clear view" button: removes every card currently shown; only exchanges with a higher
  id get cards afterwards. Data is kept in memory so tool pairing keeps working. The log
  is not touched.
- A legend on its own line: tags `YOU`, `CLAUDE`, `CALL`, `RESULT`, `ERROR`,
  `SYSTEM · THINKING`, in their colors.

When no card is visible, the page shows "No traffic yet. Run
`ANTHROPIC_BASE_URL=<page origin> claude`."

### Request card

Cards are ordered by id, newest at the top (an out-of-order arrival is inserted in place).

**Header line** (monospace, muted), in order:
- `#id` (bold), time `HH:MM:SS` (full ISO time as tooltip);
- model, or `METHOD path` for non-message requests;
- before the reply finishes: a pulsing amber dot with "streaming" (chunks arrived) or
  "waiting";
- after: `status reason` (red if ≥ 400); "unreachable" (no status) or "interrupted"
  (status but `error`) in red; duration in seconds with one decimal;
  `in X (Y cached) · out Z`, where X = input + cache-read + cache-creation tokens,
  Y = cache-read tokens (the bracket is omitted when nothing is cached), numbers ≥ 1000
  shown as `1.2k` (no decimal from 10k up); `stop_reason`;
- "non-streaming" if the request body has `stream: false`.

**Sections of a message request**, separated by lines; each has a ▸/▾ marker, a bold
name, a muted size note and an optional amber "changed" badge:

1. **System prompt** ("N chars"): collapsed. When open, each part of the system prompt
   (a string is one part; text blocks are parts) is shown as collapsible text with
   "part i of n" (only when n > 1) and a copy button.
2. **Tools** ("N tool"/"N tools"): collapsed. When open, one row per tool: name in bold
   monospace plus a one-line preview of the description (120 chars). Each row expands to
   the description (with copy) and "input schema" as JSON (or "definition" with the whole
   tool for tools without a schema).
3. **Conversation so far** ("N earlier message(s)"): collapsed; only present when there
   are earlier messages. Uses the same message rendering as below.
4. **New this turn**: expanded. Note "nothing new" if empty.
5. **Reply**: expanded.
6. **Raw**: collapsed. Shows "request headers", "request body" and "response" as JSON
   ("(not finished)" while in flight). It is rebuilt every time it is opened, so it
   reflects a reply that finished after the card appeared.

The System prompt and Tools sections get the "changed" badge when their content differs
from the **most recent earlier message request to the same model**, compared as JSON
with every `cache_control` key removed (Claude Code moves those markers between
requests). Comparing per model keeps Claude Code's side calls to a smaller model, which
use a different system prompt, from triggering the badge. The first request to a model
never shows the badge.

**New this turn** = the messages after the **last assistant message** in the request
(your prompt, or the tool results the agent sends back); everything up to and including
that assistant message is "Conversation so far". With no assistant message, every
message is new. (The assistant message is the previous reply, already shown on the
previous card.)

**Non-message requests** get a "Request body" section (expanded, JSON) if there is a
body, then a "Response" section instead of "Reply", then Raw.

Collapsed sections, collapsed blocks and collapsed JSON nodes build their content only
when first opened, so long sessions stay fast.

### Message blocks

Each content block is a box with a 4px left border and tinted background in its kind's
colors, a head row (label tag, extras, copy button on the right) and a body. Messages
are separated by 14px.

| Block | Label | Kind / look | Body |
|---|---|---|---|
| user `text` | `YOU` | blue | collapsible text, 14.5px |
| user `text` starting (after whitespace) with `<system-reminder`, `<command-name`, `<command-message`, `<command-args`, `<local-command-…` or `<user-prompt-submit-hook` | `SYSTEM` | grey, **collapsed**, head shows the first line (140 chars) | collapsible text |
| assistant `text` | `CLAUDE` | green | collapsible text, 14.5px |
| `thinking` | `THINKING` | grey, italic muted text, collapsed, first-line preview | collapsible text |
| `redacted_thinking` | `THINKING` | grey, collapsed | "(redacted)" |
| `tool_use`, `server_tool_use` | `CALL · <name>` | amber | input as JSON (raw text if it's a string) |
| `tool_result` | `RESULT · <name>` | purple | see pairing |
| `tool_result` with `is_error` | `ERROR · <name>` | red | see pairing |
| `*_tool_result` (server tools, e.g. `web_search_tool_result`) | `RESULT · <prefix>` | purple, collapsed | content as JSON |
| `image` | `IMAGE` | blue (green in a reply) | `[media type]` |
| anything else | the type in capitals | grey, collapsed | the block as JSON |

A string `content` counts as one text block. Copy buttons copy the block's text; for
calls the input as indented JSON; for results their text. Copy buttons inside a
collapsed head don't toggle it; the button shows "copied" (or "copy failed") for 1.2 s.

Result content: a string → collapsible text; a list → each `text` item as collapsible
text, `image` items as `[media type]`, others as JSON; empty → "(empty)". A result's
name comes from its call; without a known call the label is just `RESULT`/`ERROR`.

### Pairing tool calls with results

A `tool_use` appears in a reply; its `tool_result` arrives in a later request. They are
matched by id:

- Calls are registered from replies (the authoritative source) and from assistant
  messages in requests; results are registered from every request's messages, as soon
  as the request arrives, even if that section is collapsed.
- Under each call **in a Reply section**, a slot (indented under the call, inside its
  box) shows the matching result block, or "awaiting result…" in muted italics until it
  arrives. When a result arrives, every slot for that id is filled at once.
- A result outside a slot starts with an amber link `↖ answers <name>(<short input>)`,
  where short input is the first string value of the input (or the input as JSON),
  trimmed to 60 chars. Clicking it smoothly scrolls the call in its reply to the middle
  of the screen and outlines it in amber for 1.5 s; if that call isn't on screen (e.g.
  cleared), it scrolls to the card instead.
- Calls inside "Conversation so far" and "New this turn" have no slot (the result is
  shown right after them anyway).

### Reply section

- Finished, with `content` list: the assistant blocks (with slots); "(empty reply)" if
  the list is empty; plus an `ERROR` block with the `error` JSON if the message also has
  an `error`.
- API error body (`type: "error"`, or an `error` without `content`): a block
  `ERROR · <error.type or status>` with the error message as text, then the body as JSON.
- Unreachable upstream (error, no status): `ERROR` block with the error text.
- Interrupted stream (error with status): the reply, then an `ERROR` block with the text.
- Other bodies: text or JSON; no body → "(no body)".
- **While streaming**, the page assembles the chunks itself (a JS mirror of
  StreamAssembler handling `message_start`, `content_block_start`, text, input-JSON and
  thinking deltas, and `content_block_stop`) and re-renders the reply at most once per
  animation frame. Text grows with a blinking green ▍ cursor after the `CLAUDE` label;
  a tool call's input shows as raw text until its block closes and the JSON parses.
  When the `response` event arrives, the final body replaces the streamed one.
- Before any chunk: "waiting for reply…".

### Long text and JSON

- Text longer than 20 lines or 1,500 characters shows its first 10 lines (cut to 800
  characters) plus " …" and a button "show all (N lines)" (or "(N chars)" for a single
  line), which toggles to "show less". Text keeps line breaks and wraps long words.
- JSON: objects and arrays are foldable, with summary `{ N keys }` / `[ N items ]`
  (singular for 1; empty ones shown as `{}` / `[]`). Levels 0 and 1 start open, deeper
  ones closed. Keys are purple, strings teal (quoted), numbers orange, `true`/`false`/
  `null` blue. Strings containing a newline or longer than 200 characters are shown as
  collapsible text with real line breaks. Children are indented 16px with a thin left
  rule.

### Live behavior

- **Load**: fetch `/__history`, add every exchange (in-flight ones replay their `stream`
  text through the assembler), remember `seq`, then open
  `EventSource("/__events?since=<seq>")`. If the fetch fails, retry every 2 s.
- **Events**: `request` adds a card (duplicates by id are ignored); `chunk` feeds the
  card's assembler (ignored once the reply is finished); `response` sets the final
  reply, re-renders the header and reply, refreshes filter data and registers the
  reply's calls; `reset` reloads the page (so after a proxy restart the page rebuilds
  from the log).
- **Reconnect**: the browser's EventSource reconnects on its own and sends
  `Last-Event-ID`; if it gives up (closed state), the page reconnects after 2 s.
- **Scroll position**: the browser's scroll anchoring is turned off and the page keeps
  your place itself: when you're scrolled down more than 40px and anything above you
  changes (new card, streamed text, a filled result slot), the first card still on screen
  stays where it was.
- **"N new ↑" pill**: when a visible card is added while you're scrolled down, a blue
  pill below the top bar counts them; clicking it scrolls smoothly to the top; it hides
  (and the count resets) when you're back within 40px of the top.
- Parallel requests (Claude Code's background calls) each get their own card.

## Security

- Secrets are redacted as described under Redaction before anything is printed, sent or
  written.
- Everything is served on 127.0.0.1 only.
- The log contains your prompts and file contents; `.gitignore` excludes `traffic.jsonl`.

## Acceptance checks

1. Run the proxy, open the dashboard, run `claude` through the proxy, ask something that
   triggers a tool. Cards appear live, newest first, and replies grow as they stream.
2. Your prompt, the reply text, the tool call and its result are each recognizable by
   label and color without expanding anything; injected `<system-reminder>` text is a
   collapsed `SYSTEM` block, not `YOU`.
3. The tool call in reply N shows its result once request N+1 arrives; the result in
   request N+1 links back to the call.
4. System prompt, tools and earlier history are collapsed; "changed" badges show up only
   when the content changed for the same model.
5. Reload the page, or restart the proxy: history is still there (after a restart the
   page reloads itself).
6. While scrolled down, new cards don't move what you're reading and the pill appears.
7. Claude Code behaves exactly as without the dashboard (no added delay to streaming).

## Decided during the home build

These were not in the first version of this spec; they were decided while building and
are kept as built:

- "New this turn" = messages after the last assistant message (replacing a comparison
  with the previous request).
- The "changed" badge compares with the previous request **to the same model**.
- Exact colors, fonts and the legend bar (see Look).
- The page reloads itself when the proxy restarts (`reset` event).
- Replies are shown growing while they stream; tool input shows as raw text until
  complete.
- `/favicon.ico` is answered locally with 404, never forwarded.
- `.gitignore` with `traffic.jsonl` is part of the project.

## Open questions (for later)

- Group cards by session when several agents run at once?
- Diff view for a changed system prompt or tool list?
- Cost estimate per request from `usage`?
