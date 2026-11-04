"""Interactive Telegram login for the telegram-client MCP. Run it INSIDE the container, in a real terminal:

    sudo docker exec -it mcp-telegram-client python login.py           # QR (default)
    sudo docker exec -it mcp-telegram-client python login.py --code    # phone + login code
    sudo docker exec -it mcp-telegram-client python login.py --force   # replace a working session

QR: on the phone, Telegram → Settings → Devices → Link Desktop Device → scan, then type the 2FA
password here. QR is the default because the login code is known not to arrive for this account
(an existing userbot session on the same api_id is the suspect).

Writes DATA_DIR/session.string (0600). The running server notices it within a minute and
announces itself through the approval bot. The session shows in Devices as DEVICE_MODEL —
that is also where you revoke it.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from telethon.errors import SessionPasswordNeededError

import tg

QR_HINT = "Phone: Telegram → Settings → Devices → Link Desktop Device → scan this"


def _print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print("(qrcode package missing — open the URL below on the phone instead)")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


async def _qr_login(client) -> None:
    qr = await client.qr_login()
    while True:
        print(f"\n{QR_HINT}\n(expires {qr.expires.astimezone():%H:%M:%S}; a fresh one is drawn automatically)\n")
        _print_qr(qr.url)
        print(f"URL (if the terminal mangles the QR): {qr.url}")
        try:
            await qr.wait()
            return
        except (asyncio.TimeoutError, TimeoutError):
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("2FA password: "))
            return


async def main() -> int:
    ap = argparse.ArgumentParser(description="Log the telegram-client MCP into the owner's account.")
    ap.add_argument("--code", action="store_true", help="phone + login code instead of QR")
    ap.add_argument("--force", action="store_true", help="replace an existing session file")
    args = ap.parse_args()

    api_id, api_hash = os.getenv("TG_API_ID", "").strip(), os.getenv("TG_API_HASH", "").strip()
    owner = int(os.getenv("TGC_OWNER_ID", "0") or 0)
    if not api_id or not api_hash or not owner:
        print("Error: TG_API_ID, TG_API_HASH and TGC_OWNER_ID must be set (.env).", file=sys.stderr)
        return 1
    if tg.SESSION_FILE.exists() and not args.force:
        # Not checked by connecting: the server may be using this very auth key right now,
        # and two clients on one key get each other logged out.
        print(f"{tg.SESSION_FILE} already exists. If the session died, the server would have renamed it "
              "and alerted you. To replace it anyway: login.py --force", file=sys.stderr)
        return 1

    # Updates on: QR login completes through an UpdateLoginToken push.
    client = tg.new_client("", int(api_id), api_hash, updates=True)
    await client.connect()
    try:
        if args.code:
            print("Phone → the code arrives in the Telegram app (chat 'Telegram') → 2FA password")
            await client.start(phone=lambda: input("Phone (international, e.g. +995...): "),
                               password=lambda: getpass.getpass("2FA password: "))
        else:
            await _qr_login(client)
        me = await client.get_me()
        if me.id != owner:
            print(f"Error: signed in as id {me.id}, but TGC_OWNER_ID is {owner}. Logging this session out.",
                  file=sys.stderr)
            await client.log_out()
            return 1
        tg.write_private(tg.SESSION_FILE, client.session.save())
        print(f"Signed in as {tg.label(me)}. Session saved to {tg.SESSION_FILE} (0600).\n"
              f"Listed in Telegram → Settings → Devices as '{tg.DEVICE_MODEL}' — revoke it there if needed.\n"
              "The server picks it up within a minute; watch the approval bot.")
        if args.force:
            print("The previous session, if it was still alive, also remains in Devices: terminate it there.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\naborted — no session was written")
        sys.exit(130)
