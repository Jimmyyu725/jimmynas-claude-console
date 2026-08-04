# Claude Console redesign — design

**Date:** 2026-08-01
**Status:** approved, pending implementation plan
**Serves:** `cc.jimmyyu888.com` (moving from `code.jimmyyu888.com`)

## Problem

The Console lists only live tmux sessions. `GET /api/sessions` shells out to
`tmux list-sessions`, so when a session's process dies — a reboot, a crash, or a
`systemctl restart` — its card disappears and the conversation looks lost. It is
not lost: every transcript is on disk under
`~/.claude/projects/-srv-appdata/<uuid>.jsonl`. Finding and resuming one currently
requires grepping the transcripts by hand and hand-writing a `tmux new-session`
command.

Two smaller problems ride along:

- The page is functional but plain, and does not share a visual language with the
  glass portal at `jimmyyu888.com`.
- Resuming a large transcript stalls on an interactive "Resume from summary / full
  session" menu with no way to answer it from the web UI.

## Goals

1. Surface every transcript in `/srv/appdata`, not just live sessions.
2. Preview a transcript read-only, without spending tokens or starting a process.
3. Full-text search across transcripts.
4. Resume a transcript into a live, remote-controlled session in one click.
5. Match the glass portal's visual language.

## Non-goals

- Indexing other project directories (`~/Sage`, `chrome-automation/sim`). Scope is
  `/srv/appdata` only, per the owner's decision.
- A model/effort picker in the UI. The `MODEL = "opus"` alias already tracks the
  newest Opus release without a code edit, which was the underlying need. Deferred,
  not rejected.
- Editing or deleting transcripts. The history view is read-only.

## Architecture

`server.py` is 374 lines and already holds all tmux logic. Adding indexing, search,
and preview would push it past 600. Split by responsibility, each file independently
readable and testable:

| File | Responsibility | Depends on |
|---|---|---|
| `server.py` | HTTP routing, static file serving, request/response plumbing | both modules below |
| `tmux_sessions.py` | Live sessions: list, create, pause/thaw, remote-control, fast toggle, kill | tmux, cgroup freezer |
| `history.py` | Transcript discovery, metadata extraction, search, preview | filesystem only |

`history.py` has no tmux dependency and `tmux_sessions.py` has no filesystem-index
dependency; `server.py` is the only place they meet. Existing tmux code moves
verbatim — this is a file split, not a rewrite.

## History index

**Strategy: on-demand scan with an in-memory mtime cache.** The corpus is 13 files
totalling 65 MB, the largest 25 MB. SQLite + FTS5 was considered and rejected as
over-engineering at this size; a precomputed JSON manifest was rejected because it
cannot serve search and adds a systemd timer to maintain. Revisit if the corpus
passes ~50 files.

Cache key is `path -> (mtime_ns, size, metadata)`. On each `/api/history` request,
`stat()` all files; parse only those whose `(mtime_ns, size)` changed.

**Metadata extraction never reads a file whole.** For each transcript:

- **title** — first user message. Read the first 256 KB, parse line by line, stop at
  the first `type: "user"` entry with text content. Truncate to 120 chars.
- **preview** — last meaningful message. `seek()` to `max(0, size - 256 KB)`, discard
  the first (likely partial) line, parse the rest, take the last user or assistant
  text block. Truncate to 300 chars.
- **bytes**, **mtime** — from `stat()`.
- **live** — true when a running process has `--resume <uuid>` in its argv. Read
  from `/proc/*/cmdline`, not `ps`, to avoid a subprocess per request.

Cold start costs one pass over 26 × 256 KB ≈ 6.5 MB of reads, well under a second.
Steady state is 13 `stat()` calls.

## Search

`grep -c -F -e <query>` per file via `subprocess`, run concurrently with a thread
pool, 5-second timeout. Returns `{uuid, hits, snippets}` ranked by hit count.
Snippets come from `grep -m3 -o` with surrounding context. `-F` (fixed string)
avoids regex injection from user input; the query is passed as an argv element, never
interpolated into a shell string.

grep over 65 MB is single-digit milliseconds — no index needed. If a query times out,
return the results that completed plus a `partial: true` flag rather than failing the
whole request.

## API

```
GET  /api/sessions                     unchanged — live tmux sessions
GET  /api/history                      [{uuid, title, preview, mtime, bytes, live}]
GET  /api/history/<uuid>?offset=&limit= paginated messages, read-only
GET  /api/history/search?q=            {results: [{uuid, hits, snippets}], partial}
POST /api/sessions                     extended: {mode:"resume", uuid, name, summary}
```

`summary` is a boolean: `true` picks "Resume from summary", `false` picks "Resume
full session as-is". It is only consulted if the resume menu actually appears.

`uuid` is validated against `^[0-9a-f-]{36}$` and resolved inside the project
directory before any file is opened — the same containment check `_serve_static`
already applies to static paths.

## Resume flow

1. UI: **Resume** on a history card opens a dialog — session name (prefilled from
   the title, sanitised to `NAME_RE`) and a summary-vs-full choice.
2. Backend creates the tmux session with `--resume <uuid>` plus the standard
   `--model`/`--effort`/`--remote-control` flags.
3. Backend polls `tmux capture-pane` for the resume menu, up to 15 s.
   - Menu appears → send `Down` per the chosen option, then `Enter`.
   - No menu within 15 s → assume a silent resume and return success.

Step 3's dual path is load-bearing: **only transcripts above roughly 200k tokens
prompt.** Smaller ones resume silently and immediately continue any pending work, so
a backend that waits unconditionally for the menu would hang on every small session.

Two further details learned the hard way:

- Target panes by `#{session_id}` (`$N`), never by name. With no client attached,
  tmux resolves a bare name to the most recently active session, so `-t claude` can
  hit `claude-nccu`.
- `capture-pane` immediately after `send-keys` races the redraw. Capture twice before
  concluding the selection did not move.

If the requested `uuid` is already `live`, return the existing session's label
instead of launching a duplicate.

## Frontend

Reuse the portal's tokens verbatim so both pages read as one product — self-hosted
Plus Jakarta Sans, `#0d0f1e` base, three drifting radial blobs, and
`.glass { background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.13);
border-radius: 22px; backdrop-filter: blur(22px) saturate(160%) }`. Fonts are copied
into the console's `site/fonts/`, not linked across origins.

Two sections:

- **Live** — existing cards and controls (pause, fast, remote-control, delete).
- **History** — one card per transcript: title, preview, relative time, size. Actions:
  **Preview** (opens a read-only drawer) and **Resume**. Cards whose `live` is true
  render as "already running" and link to the live session instead.

A search field above History filters via `/api/history/search`, debounced 250 ms.

Accessibility and motion carry over from the portal: visible focus rings on every
interactive element, and all animation suppressed under
`@media (prefers-reduced-motion: reduce)`.

## Domain move

The `@code host code.jimmyyu888.com` matcher in `/srv/appdata/caddy/Caddyfile`
(line 180) becomes `cc.jimmyyu888.com`. Verified prerequisites: `cc.jimmyyu888.com`
already resolves to `192.168.1.50` via a wildcard record, and the matcher sits inside
the `*.jimmyyu888.com` block that holds a Cloudflare DNS-01 wildcard certificate. No
new DNS record and no new certificate. `code.jimmyyu888.com` keeps working as a
redirect for one transition period.

## Error handling

| Condition | Behaviour |
|---|---|
| Malformed JSONL line | Skip the line; never fail the whole file |
| Project directory missing | Empty list, HTTP 200 — not a 500 |
| grep timeout | Return completed results with `partial: true` |
| `uuid` not found / fails validation | 404, no filesystem access attempted |
| Resume of an already-live `uuid` | Return the existing label; do not double-launch |
| Resume menu never appears | Treat as a silent resume and succeed |

## Deploy safety

`server.py` forks the tmux server, so the tmux server lives in
`claude-console.service`'s cgroup. Under systemd's default
`KillMode=control-group`, restarting the service SIGTERMs that whole cgroup and kills
every live session — this happened on 2026-08-01 at 16:28:51 and took out four
sessions. The fix is in place at
`/etc/systemd/system/claude-console.service.d/killmode.conf` (`KillMode=process`) and
was verified on the 16:44:26 restart, where all five sessions survived.

**The deploy step must assert `systemctl show claude-console -p KillMode` returns
`process` before restarting.** The same trap applies to `Restart=on-failure`.

## Testing

**Unit — `history.py`, no tmux and no network:**

- Title extraction from a fixture whose first user message is beyond the first line.
- Preview extraction via tail-seek, including a fixture where the seek lands
  mid-line.
- A fixture containing a malformed JSONL line: parsing continues, that line is
  skipped.
- Cache returns the identical object when `(mtime_ns, size)` is unchanged, and
  re-parses when either changes.
- `uuid` validation rejects `../`, absolute paths, and non-hex input.

**Integration — against the real project directory:**

- `/api/history` returns all 13 transcripts with a non-empty title for each.
- `/api/history/search?q=NCCU` ranks `fde3a1e5-8309-4350-a35c-59b1b1533f17` first.
  This transcript has 3290 NCCU hits against 101 in the runner-up, so the ordering is
  a stable assertion rather than a coin flip.
- `/api/history/<uuid>` paginates and never returns more than `limit` messages.

**Manual, because it drives a real TUI:**

- Resume a small transcript — expect no menu and a silent start.
- Resume a large one — expect the menu, and both branches driven correctly.
- Restart `claude-console` with sessions live and confirm they survive.

## Open questions

None blocking. The model/effort picker is deferred by decision, not left unresolved.
