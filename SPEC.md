# SPEC: live traffic dashboard for proxy.py

## Goal

A browser page that shows, live, every request an agent (Claude Code) sends through
`proxy.py` and every reply it gets back. The main goal is **readability**: at a glance
you should be able to tell apart **your prompt**, **the model's reply**, **a tool call**
and **that tool's result**.

## Non-goals

- Changing traffic. The proxy keeps forwarding requests and replies untouched.
- Parsing other API formats. Only the Anthropic Messages API is understood; anything
  else is shown as pretty-printed raw JSON.
- Accounts, multiple users, remote access. The proxy listens on 127.0.0.1 only.
- Grouping requests into sessions (possible later; see Open questions).

## Decisions

| Topic | Decision |
|---|---|
| Data path | `proxy.py` itself serves the page and pushes events to it with server-sent events (SSE). One process. |
| Dependencies | Python standard library only; the page is one HTML file using plain JS and CSS. No build step, no CDN. |
| Persistence | Every finished exchange is appended to a JSONL log on disk. History survives page reloads and proxy restarts. |
| Format | Anthropic Messages API (`POST /v1/messages`, streaming and non-streaming). |
| Layout | Flat list, newest first, one card per request. Repeated history is collapsed. |

## Architecture

```
claude ──► proxy.py :8041 ──► api.anthropic.com
              │  (relay, unchanged)
              ├─► appends to traffic.jsonl
              └─► broadcasts to SSE clients
browser ◄── GET /__dashboard        (the HTML page)
        ◄── GET /__events           (SSE stream)
        ◄── GET /__history          (past exchanges from the JSONL log)
```

- The dashboard routes live under a `/__` prefix on the same port, so they can't
  clash with API paths. Requests to `/__*` are never forwarded upstream.
- New CLI options: `--log PATH` (default `traffic.jsonl` next to `proxy.py`) and
  `--no-log`.
- When it starts, the proxy prints the dashboard URL as well as the line it prints now.
- Terminal output stays as it is today.

### Events

Each exchange gets an `id` (an increasing integer that keeps counting across restarts,
picked up from the log). The proxy sends:

| Event | When | Payload |
|---|---|---|
| `request` | request received | `{id, time, request: {method, path, headers (redacted), body (parsed JSON or text)}}` |
| `chunk` | each streamed piece of the reply | `{id, data}` (raw SSE text from upstream) |
| `response` | reply finished | `{id, response: {status, reason, headers, duration_ms, body, error?}}`, where `body` is the reassembled message (see below) and `error` is set if the upstream was unreachable or the stream broke |
| `reset` | page resumed from an unknown point (proxy restarted) | none: the page reloads |

The proxy rebuilds the streamed reply into one final message object (`content` blocks,
`stop_reason`, `usage`) before sending `response` and writing the log line, so the page
and the log never have to parse SSE fragments. The page uses `chunk` only to show the
reply growing while it streams.

### Log format

One JSON object per line: `{id, time, request: {...}, response: {...}}`, the same shapes
as the events. Secrets are redacted **before** writing (see Security).

## Page structure

### Top bar
- Connection indicator (live / reconnecting).
- Filter box (plain text search across a card's contents).
- Toggles: "Hide non-message requests" (e.g. `count_tokens`, HEAD); "Pause"
  (stop adding cards; queued ones appear when you un-pause).
- "Clear view" (hides the cards; the log file is not touched).

### Request card (newest at the top)

Header line: `#id · time · model · status · duration · input/output tokens ·
stop_reason`. While streaming, a spinner replaces the status.

Sections, in order:

1. **System prompt**: collapsed by default. A "changed" badge appears if it differs
   from the previous request to the same model (`cache_control` markers ignored), so
   side calls to a smaller model don't make the badge flicker.
2. **Tools**: collapsed by default; when open, a list of tool names that each expand
   to their description and input schema. Same "changed" badge.
3. **Conversation so far**: collapsed by default; shows "N earlier messages". When
   expanded, it uses the same message rendering as below.
4. **New this turn**: expanded. The messages added since the previous request:
   usually your prompt, or the tool results the agent sends back.
5. **Reply**: expanded. The model's text and tool calls.

"New this turn" = the messages after the last assistant message (your prompt, or the
tool results the agent sends back). The assistant message before them is the previous
reply, already shown on the previous card, so it goes into "Conversation so far".

### Telling things apart

Each kind of block has its own color, label and left border, and keeps them everywhere
it appears:

| Block | Label | Look |
|---|---|---|
| Your prompt (user text) | `YOU` | blue accent, normal-weight prose |
| Model text | `CLAUDE` | green accent, prose |
| Thinking | `THINKING` | grey, italic, collapsed by default |
| Tool call (`tool_use`) | `CALL · <tool name>` | amber accent, input shown as formatted JSON |
| Tool result (`tool_result`) | `RESULT · <tool name>` | purple accent; red if `is_error` |
| System / injected text (e.g. `<system-reminder>`) | `SYSTEM` | grey, collapsed |

Colors must also work in dark mode, and every block carries its text label, so color is
never the only signal.

### Pairing tool calls with results

A `tool_use` appears in a reply; its `tool_result` arrives in the **next** request.
They are matched by `tool_use_id`:

- Under each tool call in a reply, the matching result appears inline once the next
  request arrives (until then: "awaiting result…").
- In "New this turn", each tool result shows the call it answers (tool name + short
  input) above it, with a link that scrolls to the original reply.
- The result is labeled with the tool name, looked up from its call.

### Long text and JSON

- Any text over ~20 lines or ~1,500 characters is collapsed to its first lines plus a
  "show all (N lines)" toggle.
- JSON (tool inputs, schemas, unknown bodies) is pretty-printed with syntax coloring
  and collapsible objects/arrays; nested levels deeper than 2 start collapsed.
- Strings in JSON that contain newlines are shown with real line breaks, not `\n`.
- Every section and block has a "copy" button (copies the raw text/JSON).
- "Raw" toggle on each card shows the untouched request and response JSON.

## Behavior

- On load: fetch `/__history` (most recent 200 exchanges, plus in-flight ones with their
  stream so far, and the event sequence number), then subscribe to `/__events?since=<seq>`.
- New cards slide in at the top. If you've scrolled down, the page doesn't jump;
  a "N new" pill appears that scrolls to the top.
- If SSE drops, the page reconnects and catches up using `Last-Event-ID`.
- Requests running in parallel (Claude Code sends background calls) each get their own card.

## Security

- `authorization` and `x-api-key` are cut to their first 16 chars + `…` in events and in
  the log, as the terminal output already does. The full value is never written to disk.
- Dashboard routes respond only on 127.0.0.1 (the proxy already binds there).
- The log contains your prompts and file contents; it is added to `.gitignore`.

## Acceptance checks

1. Run the proxy, open the dashboard, run `claude` through the proxy, ask something that
   triggers a tool. Cards appear live, newest first.
2. Your prompt, the reply text, the tool call and its result are each recognizable by
   label and color without expanding anything.
3. The tool call in reply N shows its result once request N+1 arrives; the result in
   request N+1 links back to the call.
4. System prompt, tools and earlier history are collapsed; "changed" badges show up only
   when they changed.
5. Reload the page / restart the proxy: history is still there.
6. Claude Code behaves exactly as without the dashboard (no added delay to streaming).

## Open questions (for later)

- Group cards by session when several agents run at once?
- Diff view for a changed system prompt or tool list?
- Cost estimate per request from `usage`?
