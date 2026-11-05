MCP server that acts as your own Telegram account; sends wait for your tap in a control bot, or go out directly with `TGC_SEND_APPROVAL=false`.

Fill `.env` from `.env.example`, press Start in the control bot, then on the VPS:
```
docker network create tgc-edge && docker network connect tgc-edge reverse-proxy
docker compose up -d --build
sudo docker exec -it mcp-telegram-client python login.py
```
Scan the QR in Telegram > Settings > Devices > Link Desktop Device; the bot says "live".

Connector URL: `https://<host>/telegram-client/<MCP_TOKEN>/`

## Files

An agent with a shell sends files in three steps:

1. `upload_link()` returns a short-lived upload URL and the exact curl line.
2. `curl -sS -T /path/to/file https://<host>/telegram-client/upload/<ticket>/` uploads one file
   (`-T "{a.png,b.pdf}"` uploads several) and prints one JSON line per file: `id`, `size`, `sha256`, `kind`.
3. `send_message(peer, text, files=[ids], as_file=false, album=true)` sends them, through the same
   approval (or direct mode) as text.

Uploads stay 2 hours in the `tgc-data` volume (`UPLOAD_*` in `.env.example`). The upload route takes
no MCP token: the ticket in its path is the credential, valid 15 minutes for up to 20 files, and it
can only add files, never send.

Telegram measures the duration of audio and video itself. The image has no ffmpeg, so this server
sends no video thumbnail of its own; `as_file=true` sends any file as a plain document.

Prerequisite: agents in sandboxes with an egress allow-list (Claude cloud sessions such as Duet) need
the upload host, `mcp.latentstate.dev` here, on that list.
