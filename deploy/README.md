# Deploying RL Prenota Online on the NAS (MassiFede)

How it runs: one **master** container (web apps, Telegram bot, scheduler), two **worker** containers (headless Chromium), and a **Tailscale sidecar** that publishes:
- the user app via **Funnel**: `https://rl-prenota.<tailnet>.ts.net/<random prefix>/`. It's reachable from the internet, but every other path is a bare 404;
- the admin console **inside the tailnet only**: `https://rl-prenota.<tailnet>.ts.net:8443/admin/`.

Nothing is published on the NAS itself, and no router ports are opened. The peak memory budget is about 1.7 GB.

## 0. One-time steps for Federico
Claude can't do these (passwords, accounts, tokens):

1. **SSH key for the NAS** (`CLAUDE.md` §3):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/nas_massifede -C "claude-code@mac"
   ```
   Add the `Host nas` block to `~/.ssh/config`, then:
   ```bash
   ssh-copy-id -i ~/.ssh/nas_massifede.pub nas
   ```
   Check with `ssh nas 'echo ok'`.
2. **Telegram bot:** @BotFather → `/newbot` (e.g. *RL Prenota Online*, username `RLPrenotaBot`). Keep the token for step 2.3.
3. **Tailscale** ([admin console](https://login.tailscale.com/admin)):
   - DNS → enable **MagicDNS** and **HTTPS certificates**;
   - Access controls → add the Funnel attribute for the sidecar's tag, then create the auth key with that tag:
     ```json
     "tagOwners": {"tag:rl-prenota": ["autogroup:admin"]},
     "nodeAttrs": [{"target": ["tag:rl-prenota"], "attr": ["funnel"]}]
     ```
   - Settings → Keys → **Generate auth key**: reusable = no, ephemeral = no, tags = `tag:rl-prenota`.
   - Note your tailnet name (e.g. `tail1234ab.ts.net`).

## 1. Folders
- **Code:** `/share/CACHEDEV1_DATA/.qpkg/container-station/data/application/rl-prenota/`. Container Station shows the project there.
- **Data:** `RL_Prenota_Online/` inside the *Fede private* share, set as `RLP_HOME`. It holds:
  - `data/`: the database and encrypted backups;
  - `secrets/`: keys and tokens, all `chmod 600`;
  - `tailscale/`: the sidecar's state.

## 2. First installation (over SSH)
```bash
# 2.1 copy the code (from the Mac)
rsync -a --delete --exclude .git --exclude .venv ./ nas:/share/CACHEDEV1_DATA/.qpkg/container-station/data/application/rl-prenota/

# 2.2 on the NAS: settings and secrets
cd /share/CACHEDEV1_DATA/.qpkg/container-station/data/application/rl-prenota/deploy
cp .env.example .env        # fill RLP_HOME, RLP_PUBLIC_ORIGIN, RLP_BOT_USERNAME, RLP_UID/RLP_GID (id -u / id -g)
set -a; . ./.env; set +a
mkdir -p "$RLP_HOME"
docker compose build master
docker run --rm --user "$RLP_UID:$RLP_GID" -v "$RLP_HOME":/home rl-prenota-master:local \
  python -m rlprenota.master.manage init /home

# 2.3 the two secrets only you have (never in git, never on the command line history: use an editor)
vi "$RLP_HOME/secrets/bot_token"          # the @BotFather token
vi "$RLP_HOME/secrets/tailscale.env"      # TS_AUTHKEY=tskey-auth-...
chmod 600 "$RLP_HOME/secrets/bot_token" "$RLP_HOME/secrets/tailscale.env"

# 2.4 start
docker compose up -d --build
docker compose ps && docker stats --no-stream

# 2.5 first admin: open the printed link in Telegram
docker compose exec master python -m rlprenota.master.manage invite Federico --login fede --admin
```
Then open `https://rl-prenota.<tailnet>.ts.net:8443/admin/` from a device in the tailnet and log in as `fede`. Invite other people from there. The user link is printed in the welcome message the bot sends after `/start`.

## 3. Updates
```bash
rsync -a --delete --exclude .git --exclude .venv ./ nas:/share/CACHEDEV1_DATA/.qpkg/container-station/data/application/rl-prenota/
ssh nas 'cd /share/CACHEDEV1_DATA/.qpkg/container-station/data/application/rl-prenota/deploy && docker compose up -d --build'
```
Searches survive restarts: leases expire and are requeued.

## 4. When something goes wrong
- **"Il portale è cambiato" alert on Telegram:** all searches are paused automatically. Check the portal, update `rlprenota/core/portal.py` (the alert names the missing selector), deploy, then press *Riprendi tutte le ricerche* in the admin console.
- **Logs:** `docker compose logs --tail 200 master worker`. They are redacted: no codice fiscale, ricetta, phone numbers or emails.
- **Restore a backup:**
  ```bash
  docker compose stop master
  docker compose run --rm master python -c "from rlprenota.master.backup import restore_database; from rlprenota.master.crypto import Crypto; restore_database('/data/backups/<file>.db.enc', Crypto.from_key_file('/run/secrets/master_key'), '/data/restored.db')"
  ```
  Then replace `master.db` and start the master again.
- **Chromium sandbox:** if workers fail to start Chromium on the QTS kernel even with `chrome-seccomp.json`, set `RLP_CHROMIUM_NO_SANDBOX: "1"` on the worker. Container isolation stays in place: non-root, read-only filesystem, no capabilities.
- **Emergency stop:** `docker compose stop`. Data and secrets are untouched.

## Security notes
- Health data at rest is encrypted with AES-256-GCM. The key is `secrets/master_key`: **back it up offline**, because without it the database and the backups are unreadable.
- Personal login data is wiped when a search is booked, expires, or is deleted.
- The `secrets/` folder and `.env` must never be committed. `.gitignore` covers `data_file.py` and `stato_ricerca.json`; deploy secrets live only on the NAS.
