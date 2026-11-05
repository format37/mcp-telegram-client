# Kickoff: file attachments for `send_message`

Status: design agreed on 2026-11-05 between the owner and Claude, revised the
same day after Duet's review (issue #1). Implemented and deployed the same day
(86b26df..), preview button included; what the real account showed is under
"Step 5 results" near the end. Open: the upload test from a Duet cloud session.
This was the brief for the implementing session.

## Goal

An agent with a shell and this connector (Duet, Claude Code, anything similar)
can send files from the owner's Telegram account: charts, PDFs, photos, audio,
any binary it produced locally. Today `send_message` carries text only.

The flow:

1. The agent calls `upload_link()` and gets a short-lived upload URL.
2. From its shell it uploads each file with one curl command and gets back a
   JSON record: `id`, `size`, `sha256`, detected `kind`.
3. It calls `send_message(peer, text, files=[ids], as_file=..., album=...)`.
   The approval gate (or direct mode) applies exactly as it does for text.

Dropped alternatives: email ingestion (the sender is forgeable and it would put
mail credentials into the most fenced container we run) and inline base64 in
tool arguments (every byte costs output tokens). Other file sources worth
having later are listed at the end. None of them is in this scope.

## What exists today: read these first

- `backend/main.py`: config block at the top; `send_message` with the two
  modes; `_execute_draft`; `_status_text`; the `_Redact` log filter;
  `INSTRUCTIONS` text; `TokenAuthMiddleware` and `routes` at the bottom.
- `backend/approvals.py`: `Draft`, `card_text`, `Approvals.submit`,
  `_on_callback`, `_execute`, the control bot commands.
- `backend/tg.py`: `send_text` (pacing, flood mapping, budget), `SendLog`,
  `write_private`, `DATA_DIR`.
- `docker-compose.yml`: read-only root, tmpfs `/tmp` of 64 MB, `/app/data`
  named volume, 512 MB memory cap, Caddy in front on the `tgc-edge` network.
- `tests/`: `conftest.py` sets the env and provides `FakeBotHTTP`;
  `_direct_probe.py` runs direct mode in a subprocess; `test_direct_mode.py`
  enumerates the tool names and bans approval vocabulary from direct-mode text.

## Decisions

### Upload endpoint

`PUT https://mcp.latentstate.dev/telegram-client/upload/<ticket>/<filename>`
with the raw file as the body. `POST` is accepted with the same meaning.

- Raw body, not multipart: it streams straight to disk, so nothing is buffered
  under the 512 MB cap or spooled into the 64 MB tmpfs, and there is no new
  dependency. curl does it with one flag.
- The tool result gives the agent the exact command:
  `curl -sS -T /path/to/file <url>/`. curl appends the local basename when the
  URL ends with `/`, and `curl -sS -T "{a.png,b.png}" <url>/` uploads several.
  `curl --data-binary @file <url>/<name>` works as well.
- Filenames arrive percent-encoded and must be decoded exactly once. Checked on
  2026-11-05 with curl 8.5 against uvicorn and Starlette:

  | local file | request path from curl | Starlette path param |
  |---|---|---|
  | `my chart.png` | `my%20chart.png` | `my chart.png` |
  | `отчёт.pdf` | `%d0%be%d1%82%d1%87%d1%91%d1%82.pdf` | `отчёт.pdf` |
  | `a%41#b.txt` | `a%2541%23b.txt` | `a%41#b.txt` |

  uvicorn already decodes the path, so the handler takes the path param as is
  and never calls `unquote` again: a second decode turns `a%41` into `aA`.
  Declare the route as `/upload/{ticket}/{name:path}` so a decoded `%2F` does
  not break routing, then keep only the last `/` segment. Normalise the name
  to Unicode NFC, because macOS hands curl decomposed Cyrillic (`е` plus a
  combining diaeresis instead of `ё`).
- Response `201` with JSON:
  `{"id":"f_3fa9c21b7d04","name":"chart.png","size":123456,"sha256":"...",
  "mime":"image/png","kind":"photo","width":1600,"height":900,
  "expires":"2026-11-05T14:02:00+04:00"}`. `width` and `height` only for images.
- Errors: `404` for an unknown, expired or used-up ticket (one answer for all
  three); `400` for no filename or an empty body, with the curl hint in the
  body; `413` over `MAX_UPLOAD_MB`, and the partial file is deleted; `507` when
  the store is full; `423` while the owner has `/lock`ed.
- The route bypasses the MCP-token check in `TokenAuthMiddleware` (path
  segments `telegram-client/upload/...`); the ticket is the credential. Put the
  route in `routes` before the `Mount("/")`.
- Public base URL: new env `TGC_PUBLIC_URL`, defaulting to
  `https://<first host in MCP_ALLOWED_HOSTS><BASE_PATH>`. The current deploy
  therefore needs no new variable.

### Tickets: the `upload_link` tool

- `upload_link()` returns the URL, the curl line, the limits and the expiry.
  It refuses while locked.
- A ticket is `secrets.token_urlsafe(32)`, valid for `UPLOAD_LINK_TTL_MIN`
  minutes (default 15), good for up to 20 files. It lives in memory only; a
  restart invalidates it and the agent simply asks for another.
- It is deliberately not the MCP token. The URL lands in transcripts and shell
  history, and an upload-only, short-lived ticket is all it may grant.
- Redaction: the ticket sits in the request path, and uvicorn's access log
  prints paths. Extend `_Redact` with a regex pass that turns
  `/upload/<anything>/` into `/upload/***/`, since tickets are dynamic. Test it.
- Caddy, checked on 2026-11-05: the VPS runs Caddy 2.8.4, and its Caddyfile has
  no `log` directive on any site, in line with the house "no access log" rule.
  So Caddy writes no access log for uploads today. Keep it that way. If a `log`
  directive is ever added to the `mcp.latentstate.dev` block, add
  `log_skip /telegram-client/upload/*` with it.
- Residual, accepted: Caddy's error logger still records the request URI in
  the `reverse-proxy` container's output when a proxied request fails, for
  example a 502 while this container restarts. A leaked ticket allows uploads
  only, never sends, within the store caps, until it expires.
- The `upload_link` result also tells the agent what a sandbox block looks like.
  If curl reports a 403 from a proxy or a failed CONNECT, the upload host is
  missing from the sandbox's network allow-list. The agent tells the owner and
  does not retry in a loop.

### Storage: `backend/uploads.py`

- Bytes go to `DATA_DIR/uploads/<id>/<name>` (0600, written under a temp name
  and moved into place like `write_private`), metadata to
  `DATA_DIR/uploads/<id>.json`. On startup rebuild the index from the sidecars
  and sweep.
- The stored path must end with the sanitised original name. Telethon decides
  photo or document from the path's extension, guesses the mime type from it,
  and shows the recipient the path's basename as the filename. A bare
  `uploads/<id>` would go out as an extensionless document named after the id.
- `id` is `"f_" + secrets.token_hex(6)`. `sha256` is computed while streaming.
- Lifetime `UPLOAD_TTL_MIN` (default 120). A sent file is not deleted: the
  agent may send the same chart to two people. It expires. A file referenced
  by a pending draft gets its expiry pushed past the draft's expiry at submit.
- Caps: `MAX_UPLOAD_MB` per file (default 100), `UPLOAD_STORE_MB` in total
  (default 1000). A sweep task started in `lifespan` runs every 60 s.
- `kind` comes from the extension and mime type (`mimetypes`). Kinds: `photo`
  (jpeg, png), `video` (mp4, mov, webm, mkv), `audio` (mp3, m4a, ogg, flac,
  wav), `gif`, else `document`. Photos are only jpeg and png because Telethon
  sends only those extensions as photos. Other images, such as webp, heic and
  bmp, are documents and always go with `force_document=True`, since a webp
  sent without it can turn into a sticker.
- Image dimensions come from the header only: `Image.open(path)` and read
  `.size`. Never call `load()`, `verify()`, `convert()` or `thumbnail()`, which
  decode pixels; a decompression bomb would push the container past its
  512 MB memory cap. `Image.open` itself raises `DecompressionBombError` on a
  huge header and only warns on a large one, so open it with
  `DecompressionBombWarning` turned into an error. Any failure means `document`
  with no dimensions.
- The header check also guards the send. Telethon decodes and resizes images it
  sends as photos once they exceed 2560 px. Only images that passed the photo
  limits below reach that path, which bounds the decode at about 5000 x 5000.
- The filename is reduced to a basename, control characters stripped,
  normalised to NFC, cut to 200 bytes of UTF-8 at a character boundary with the
  extension kept, and never used as a path on its own. A Cyrillic name is two
  bytes per letter, so the cut matters at about 100 letters.
- `list_uploads()` tool: id, name, size, sha256, kind, expiry. It exists for
  recovery when the agent lost its shell output. `/status` in the bot and
  `_status_text` get a line `uploads: N files, X MB`.

### `send_message` changes

Three new optional parameters, applied per call. One call stays one approval
and one message (an album is one message).

```
files: list[str] = []     # upload ids from the JSON the uploads returned
as_file: bool = False     # True: every file goes as a document (full resolution, original bytes)
album: bool = True        # True: one grouped message, up to 10 per album; False: one message per file
```

Rules:

- `text` may be empty when `files` is non-empty. Otherwise it is an error, as
  today.
- Caption: the text is the caption of the album, or of the first file when
  `album=False`, when its parsed length fits `CAPTION_MAX` (env, default 1024;
  the owner has Premium and may raise it). Otherwise the text goes out first as
  its own message (markdown, `reply_to`, as today) and the files follow it with
  no caption. The result says which happened.
- Parsed length means `tg.ulen` of the text Telegram will store, after the
  markdown markup is removed: `telethon.extensions.markdown.parse(text)[0]`,
  or the raw text when `plain=True`. Markup can be most of the raw length. A
  sample with bold, italic, code and one link measured 77 characters raw and
  28 parsed. The card limit `MAX_DRAFT_CHARS` still applies to the raw text,
  because the card shows it raw.
- "As media" (`as_file=False`): files of kind `photo` go as photos (Telegram
  recompresses) when the file is at most 10 MB, its header gave dimensions,
  width plus height is at most 10000 px and the aspect ratio is at most 20:1;
  otherwise they go as documents with a note in the result and on the card.
  Videos go as video with `supports_streaming=True`, audio as audio, gif as
  animation, everything else as a document. `as_file=True` sets
  `force_document=True` for all.
- Album grouping: Telegram allows photos and videos together, audio only with
  audio, documents only with documents. `album=True` over a set that mixes
  photo or video with document or audio raises a `ToolError` that names both
  fixes: `as_file=true` makes one document album, or `album=false`. More than
  10 files are split into consecutive albums of at most 10, caption on the
  first.
- GIFs never go into an album. Telegram does not group animations, and
  Telethon's `send_file` documentation says so. With `album=True`, a gif in a
  set of two or more files raises the same `ToolError`, whether the others are
  photos, videos or more gifs. A gif alone goes out as one animation. With
  `as_file=True` a gif is a document and groups with documents; confirm on the
  real account in step 5 that Telegram keeps it a file inside the album.
- Budget: an album is one send. `album=False` with N files is N sends:
  `sendlog.check(N, ...)` up front, `pace()` between messages. A
  `FloodWaitError` midway pauses sends as in `send_text`; the error says how
  many went out.
- An unknown or expired id raises a `ToolError` listing the ids it does not
  know. Nothing is sent.
- `reply_to`, `silent` and `plain` apply to the file message(s). `no_preview`
  is ignored for captions.
- Implement as `tg.send_files(client, entity, paths, *, caption, as_file,
  album, reply_to, plain, silent, log, is_new)` next to `send_text`, sharing
  its error mapping. Telethon: `client.send_file(entity, [paths],
  caption=..., force_document=..., supports_streaming=..., reply_to=...,
  silent=..., parse_mode=...)`; a list of paths makes an album and the call
  returns a list of messages.
- Known limitation, accepted: there is no ffmpeg in the image, so a video sent
  as media carries no duration or thumbnail. The agent sends `as_file=True`
  when that matters.

### Approval mode

- `Draft` gains `files` (resolved records: id, name, size, kind, and how each
  will go out), `as_file` and `album`. Paths are resolved again at execution;
  a file that vanished between submit and the tap fails the draft with a clear
  reason.
- `card_text` adds a paperclip block, one line per file, for example
  `📎 chart.png · 1.2 MB · photo (compressed)` and
  `📎 report.pdf · 3.4 MB · file`, followed by `album of 3` or
  `3 separate messages`, and whether the text is a caption or goes out as its
  own message. The card must still fit one bot message, so the text limit
  becomes `MAX_DRAFT_CHARS` minus the size of the files block.
- Preview button, recommended and allowed as a second step: a `👀 Files`
  button under the ✅ / ❌ row makes the bot send the owner the actual files
  through the Bot API (`sendDocument` multipart; the bot limit is 50 MB, larger
  files answer "too big to preview, size and sha256 are on the card"). Owner
  only, like the other buttons.
- Direct mode: the same send path; the silent log line lists the files. No
  separate approval flag for files in this iteration.
- `/lock` cancels pending drafts as today. Uploads stay until they expire.

### Agent-facing text

- `INSTRUCTIONS` gets a "Files" paragraph for both modes: `upload_link`, then
  curl, then the ids into `send_message(files=...)`; `as_file` for full
  resolution; `album` is on by default; compare `sha256` with `sha256sum` when
  it matters; an upload id is not a message, nothing is sent until
  `send_message`.
- `_SEND_ARGS` documents the three parameters.
- `test_agent_facing_text_has_no_approval_vocabulary` bans "draft", "approv",
  "tap" and "card" from direct-mode text: write the new shared text without
  them. `test_no_draft_tools` enumerates tool names: add `upload_link` and
  `list_uploads`.

### Infrastructure

- Caddy (the `ovh` skill), checked on 2026-11-05: the `mcp.latentstate.dev`
  block has no `request_body max_size`, so Caddy's default of unlimited applies
  and `MAX_UPLOAD_MB` in the app is the only cap. No Caddy change is needed.
  The access log decision is under "Tickets".
- Agent sandboxes: Claude cloud sessions such as Duet reach the internet
  through an allow-listing proxy. The host in `TGC_PUBLIC_URL` must be on the
  environment's network allow-list before an agent there can upload. The owner
  added `*.latentstate.dev`, which covers `mcp.latentstate.dev`; it does not
  cover the bare `latentstate.dev`, and nothing here needs that.
- No compose change: uploads live in the `tgc-data` volume. `mem_limit` stays;
  streaming keeps memory flat, and Telethon uploads from a path in parts.
- `.env.example` gains `TGC_PUBLIC_URL`, `MAX_UPLOAD_MB`, `UPLOAD_STORE_MB`,
  `UPLOAD_TTL_MIN`, `UPLOAD_LINK_TTL_MIN` and `CAPTION_MAX`, commented like
  the existing entries.
- README gets a "Files" section with the three-step flow and the curl line,
  and a prerequisite line: agents in sandboxes with an egress allow-list need
  the upload host on that list. Bump `APP_VERSION` in `tg.py` to `tgc-mcp 1.1`.

## Tests

`tests/test_uploads.py`, a `TestClient` against the real app, module-scoped
like the `live` fixture in `test_server.py`:

- unknown, expired and used-up tickets give 404; locked gives 423; a missing
  filename gives 400 with the curl hint
- a body streamed past `MAX_UPLOAD_MB` gives 413 and leaves no file behind;
  the store cap gives 507
- `sha256` and `size` match the bytes; the sidecar is 0600; a restart reloads
  the index; the sweep removes expired files and sidecars
- filename sanitation: `../x`, control characters, a 300-byte name
- real names survive the trip: requests to the encoded paths in the table under
  "Upload endpoint" store `my chart.png`, `отчёт.pdf` and `a%41#b.txt`, never
  a `%D0%BE...` form; a decomposed `отчёт.pdf` is stored in NFC; a 300-byte
  Cyrillic name is cut at a character boundary and keeps `.pdf`
- the stored file sits at `uploads/<id>/<name>`, so the path keeps the real
  extension
- a PNG whose header claims 100000 x 100000 pixels is stored as a `document`
  with no dimensions, without decoding, and memory stays flat
- the ticket is redacted from log records
- `list_uploads` shows the file; `upload_link` refuses while locked

`tests/test_send_files.py`, with a fake client that records `send_file`
calls the way `_direct_probe.py` fakes `send_text`:

- 12 photos with `album=True` give two `send_file` calls (10 and 2), caption on
  the first, budget counted as 2
- a photo plus a PDF with `album=True` raises the error naming `as_file` and
  `album=false`; with `as_file=True` it becomes one document album
- a photo plus a gif, and two gifs, with `album=True` raise the same error; a
  gif alone goes as one animation; a photo plus a gif with `as_file=True` is
  one document album
- the fake client receives paths that end with the original names, such as
  `.../my chart.png`
- a markdown-heavy text whose raw length is over `CAPTION_MAX` but whose
  parsed length fits becomes the caption; one whose parsed length is over goes
  out as its own message; with `plain=True` the raw length counts
- a webp goes as a document even with `as_file=False`
- `album=False` gives N calls, N budget, pacing between them
- long text goes out as a message first and the files follow without a
  caption; short text becomes the caption
- empty text with files is accepted; empty text without files is still an
  error
- an unknown id raises and nothing is sent
- approval mode: the card lists the files and the grouping; ✅ sends once with
  the files; a file deleted before ✅ leaves the draft `failed` with the reason
- direct mode via `_direct_probe.py`: one send with a file, and the silent log
  line names the file

Run the suite the way the repo runs it today (`pytest` from `tests/`), and
keep every existing test green.

## Order of work

1. `backend/uploads.py`: store, tickets, sweep, HTTP handler; the middleware
   exemption, the route and the redaction; tests.
2. `tg.send_files` and kind detection; unit tests with a fake client.
3. `send_message` parameters, `Draft` fields, card, executor, direct mode;
   tests.
4. Agent text, README, `.env.example`, version bump.
5. Deploy on the VPS (`docker compose up -d --build`, see README) and test end
   to end against a chat the owner names. Saved Messages stay blocked unless
   `TGC_ALLOW_SAVED_MESSAGES=true`. Prerequisite: the upload host is on the
   Duet environment's network allow-list (see "Infrastructure").
   - From Claude Code on the owner's machine, through Caddy only: upload a
     1 MB and a roughly 100 MB file.
   - From a Duet cloud session, through its sandbox proxy as well: the same two
     sizes. That proxy's request body limit is unknown. If it cuts uploads
     short, record the limit in the README and in the `upload_link` text.
   - With real curl: `my chart.png` and `отчёт.pdf` show their real names on
     the card and in the chat.
   - On the real account: a photo plus a gif with `as_file=True` arrives as one
     document album, the gif kept as a file.
6. The preview button, if it was not done in step 3.

Small commits on `main`, as before.

## Step 5 results (2026-11-05)

Production runs with `TGC_SEND_APPROVAL=false`, so the sends went out directly,
to the owner's own @id37bot. The control bot's chat cannot take them: the
server blocks it by design.

- From Claude Code on the owner's machine, through Caddy: a 1 MB and a 95 MB
  upload, both with sha256 equal to `sha256sum`; 95 MB took 19 s. An upload
  over the 100 MB cap gets the JSON 413 after 64 KB, so curl never sends the
  body. The access log shows `/upload/***/`.
- Real curl names: `my chart.png` and `отчёт.pdf` arrived under those names.
- A photo, a PDF and a GIF with `as_file=true` arrived as one album of files,
  the GIF kept as a file. A photo alone arrived compressed, markdown caption
  parsed.
- Audio and video sent with duration 0 showed 0:04: Telegram measures the
  duration itself, so that part of the "known limitation" does not hold.
- A GIF now goes with `documentAttributeImageSize` and
  `documentAttributeAnimated`, as Telegram Desktop sends it; Telethon adds
  neither. A 250 KB ffmpeg GIF then arrived as an MP4 animation. A 4.6 KB
  two-colour test GIF stayed a plain file however it was sent.
- A 95 MB file went to Telegram in 140 s; the container peaked at 111 MiB of
  its 512 MiB.
- A restart rebuilt the upload index from the sidecars (7 files kept).

## Later, not now

- Re-send media that is already in Telegram by message reference: no bytes
  move and there is no size cap.
- The control bot as the owner's inbox: a file shared to it from the phone
  becomes an attachable id.
- Fetch by URL, with a private-address guard, for files that other MCP servers
  return as links.
- Voice notes (`voice_note=True`) and video thumbnails (needs ffmpeg in the
  image).
- The same decompression-bomb guard for `get_media`. Its `_to_jpeg` decodes
  images from any chat, and anyone can post an image file there.
- The same feature in the local `telegram-client` skill. `tgc.py` already runs
  on the owner's machine, so there it would just take paths.
