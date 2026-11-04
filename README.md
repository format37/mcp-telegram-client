MCP server that acts as your own Telegram account; sends wait for your tap in a control bot, or go out directly with `TGC_SEND_APPROVAL=false`.

Fill `.env` from `.env.example`, press Start in the control bot, then on the VPS:
```
docker network create tgc-edge && docker network connect tgc-edge reverse-proxy
docker compose up -d --build
sudo docker exec -it mcp-telegram-client python login.py
```
Scan the QR in Telegram > Settings > Devices > Link Desktop Device; the bot says "live".

Connector URL: `https://<host>/telegram-client/<MCP_TOKEN>/`
