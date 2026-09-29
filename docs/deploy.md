# Deploy

The production bot runs on a small VPS (1 vCPU, 2 GB RAM) that it shares with other projects.
Images are built on a developer machine and shipped over ssh: `deploy/deploy.sh` builds
`sublya-bot` for linux/amd64, loads it on the server and restarts the stack in
`/opt/sublya/bot`.

## Why there's a tunnel

The server is in Russia: `api.telegram.org`, Telegram's data centres and OpenRouter don't
answer from it. The host runs xray with a SOCKS inbound on port 1080. The Bot API server
talks MTProto and has no setting for a SOCKS proxy, so instead of teaching every client about
the proxy, three containers share one network namespace:

- `tunnel` runs [tun2socks](https://github.com/xjasonlyu/tun2socks): a TUN device with the
  default route, and everything that goes into it leaves through `socks5://host.docker.internal:1080`;
- `telegram-bot-api` listens on `127.0.0.1:8081` inside that namespace;
- `bot` talks to it at `127.0.0.1:8081` and reaches OpenRouter the same way.

If the bot stops getting updates, check the proxy first, from the server:

```bash
curl -x socks5h://127.0.0.1:1080 -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org
```

## First deploy

1. On the server, create `/opt/sublya/bot/.env` from `.env.example`. `TELEGRAM_API_URL` and
   `DATA_DIR` are set by the compose file.
2. If the bot has been polling the public Bot API, log it out once:
   `curl https://api.telegram.org/bot$BOT_TOKEN/logOut`. If it ran against another local
   Bot API server, call `close` on that one instead.
3. `deploy/deploy.sh`.

## Files on the server

```
/opt/sublya/bot/
  docker-compose.yml   # copied by deploy.sh
  .env                 # secrets, made by hand
  data/                # SQLite and job files; jobs are removed after 24 hours
```

The bot needs no open ports: it polls Telegram through the local Bot API server. The site is
on GitHub Pages, see [sublya/site](https://github.com/sublya/site).
