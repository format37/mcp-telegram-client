MCP server that acts as your own Telegram account; every send waits for your tap in an approval bot.

Fill `.env` from `.env.example`, press Start in the approval bot, then on the VPS:
```
docker network create tgc-edge && docker network connect tgc-edge reverse-proxy
docker compose up -d --build
sudo docker exec -it mcp-telegram-client python login.py
```
Scan the QR in Telegram > Settings > Devices > Link Desktop Device; the bot says "live".

Connector URL: `https://<host>/telegram-client/<MCP_TOKEN>/`
