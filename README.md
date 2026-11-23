[English](README.md) | [Русский](README.ru.md)

# x-discord-media-bot

Self-hosted Discord bot and lightweight web gallery for automatically archiving and relaying media from **X (formerly Twitter)** posts.

## About the Project

I created this project as a self-hosted replacement for public bots like *SaucyBot*, which truncate carousel posts at 4 attachments even though X supports up to 20 images and videos per post. My goal was an autonomous tool for a home server (Proxmox LXC / Linux) with zero reliance on third-party public APIs, webhook-based sender relay, and a fast local web archive for viewing and downloading.

**How it was built:** all architecture, user flows, resource safeguards, and technical specifications were designed by me, while the code was written in collaboration with AI (**Google Gemini**).

---

## Key Features

* **100% Local Media Extraction:**
  * Downloads originals directly via `gallery-dl` (with automatic fallback to `yt-dlp`).
  * Supports up to 20 attachments in original order and quality, including age-restricted posts via `cookies.txt`.
  * **Smart Quote Tweet handling:** downloads only the main tweet's media if present; if the main post has no media of its own, automatically pulls media from the quoted post with clear attribution (`↪ Quoted media from @author`).
* **Clean Discord Relay via Webhooks:**
  * Publishes messages using the sender's **local server nickname and avatar** via channel webhooks (supports both regular text channels and Threads; gracefully falls back to regular bot messages if `Manage Webhooks` is missing).
  * **Smart comment filtering:** ignores messages whose text exceeds `MAX_COMMENT_LENGTH` (default: 50 characters). All URLs are excluded from the length count, while **non-X external links** (e.g., store or YouTube links) are preserved in the relayed message.
  * Displays multi-link counters (`1/3`), X author & post text (safely truncated under Discord's 2000-character limit with reserved space for future cleanup edits), post stats (likes, reposts, replies, views) with dynamic Discord `<t:...:f>` timestamps, and direct links to both the local archive and the original X post.
  * Suppresses duplicate embeds inside the bot's own message via `<...>` angle brackets while keeping OpenGraph tags on the web page so bare gallery URLs still generate rich previews if shared elsewhere.
  * Automatically adds **`❌`** (author or moderator deletion) and **`🩶`** (quick like) reactions.
* **Autoplaying GIFs & Modern Compression:**
  * Automatically converts X's muted MP4-GIFs into real looping **`.gif`** files (via `ffmpeg` palette generation) for Discord attachments so they autoplay inline, while keeping the smooth original `.mp4` on disk for the web gallery.
  * Compresses oversized videos in a temporary directory using modern codecs (**AV1 `libsvtav1`**, **VP9 `libvpx-vp9`**, or **HEVC `libx265`**) with configurable speed/quality presets (`VIDEO_PRESET`), and compresses oversized static images to **WebP** (avoiding JPEG gradient banding).
  * Performs a final byte-size verification before upload, splits batches on HTTP 413, and skips attaching ultra-long videos that would become unwatchable under Discord's file size limit.
* **4-Mode Web Gallery (`FastAPI` + Vanilla JS):**
  * Displays the **full, untruncated** X post text with clickable links, post stats, and a one-click **`Download all (.zip)`** asynchronous streaming archive (constant RAM, no extra archive stored on disk).
  * **Four display modes:** `Carousel`, `Comic`, `Smart Tiles` (Masonry), and DPR-aware `Grid` (videos play only in fullscreen Lightbox).
  * Loads lightweight **WebP previews** immediately while preloading originals in the background for instant Lightbox viewing.
  * **Live storage footer (optional):** dynamically displays free disk space, cleanup reserve, and the post's exact position in the FIFO deletion queue (can be disabled via `SHOW_STORAGE_STATUS=false`).
* **Security, Isolation & Resource Safeguards:**
  * **Privilege isolation:** `scripts/install.sh` runs the service under an unprivileged `xbot` system user with `systemd` sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`), keeps `.env` root-only (`600`), and strips `DISCORD_TOKEN` from `ffmpeg`/`gallery-dl`/`yt-dlp` subprocess environments.
  * **Live disk quota & dynamic FIFO cleanup:** monitors disk space and active download size in real time (`MAX_POST_DISK_PERCENT`, default `50%` of available bot disk budget), purges oldest posts mid-download if needed, and cleanly aborts oversized posts with a chat notice before the disk fills up.
  * **Access & web protection:** blocks DMs by default (`ALLOW_DMS=false`), supports optional `ALLOWED_GUILD_IDS`, enforces a strict media-only whitelist (blocking `meta.json` disclosure), limits concurrent ZIP downloads per client IP (`MAX_CONCURRENT_DOWNLOADS_PER_IP=10`), supports restricting direct web port access to `TRUSTED_PROXY_IPS` (`RESTRICT_WEB_TO_TRUSTED_PROXIES`), and sets `nosniff` / `no-referrer` / `SAMEORIGIN` headers.
  * **Queues & RAM guard:** strict per-channel/thread `asyncio.Lock`, persistent `data/queue.json`, `/proc/meminfo` RAM check (`MIN_FREE_RAM_MB`), and `nice -n 10` for `ffmpeg`.
* **Proxy & Reverse Proxy Friendly:** routes outbound Discord and X traffic (including DNS via `socks5h://` / `rdns=True`) through a local `SOCKS5`/`HTTP` proxy while serving incoming gallery traffic behind Nginx Proxy Manager.

---

## Tech Stack

* **Backend & Bot:** Python 3.11+, `discord.py`, `FastAPI`, `uvicorn`, `Jinja2`, `aiohttp-socks`
* **Extraction & Media Processing:** `gallery-dl`, `yt-dlp`, `ffmpeg` / `ffprobe`, `Pillow`
* **Frontend:** Native HTML5 / CSS3 / Vanilla JS (zero external CDNs or frameworks)

---

## System Requirements

Tested in a minimal Debian/Ubuntu LXC container:

* **Minimum:** 1 vCPU, 1 GB RAM, 3 GB disk space (~2.2 GB for OS/environment + ~800 MB for cached media, enough for 200+ posts when setting `MIN_FREE_SPACE_GB=0.5`).
* **Recommended:** 2+ vCPU, 2 GB RAM, 20+ GB disk space (for faster AV1/HEVC video encoding and larger local media retention).
* **OS:** Debian 12+, Ubuntu 22.04+, or another compatible Linux distribution with Python 3.11+ and `ffmpeg`.

> **Tip for 1 vCPU / 1 GB RAM / 3 GB disk setups:** Set `MIN_FREE_SPACE_GB=0.5`, `MAX_CONCURRENT_JOBS=1`, and `MIN_FREE_RAM_MB=256` in `.env`, and consider `VIDEO_PRESET=6` (or `VIDEO_CODEC=libx265` with `VIDEO_PRESET=medium`) for faster encoding on a single core.

---

## Quick Deployment (Debian / Ubuntu / Proxmox LXC)

### 1. Discord Bot Setup
1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) and copy the token from the **Bot** tab (disable **Public Bot** if this is a private instance).
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
3. Invite the bot to your server with the following permissions:
   `View Channels`, `Send Messages`, `Send Messages in Threads`, `Manage Messages`, `Manage Webhooks`, `Embed Links`, `Attach Files`, `Read Message History`, `Add Reactions`.

### 2. Install in LXC Container
Clone the repository and run the installer (creates the isolated `xbot` user, installs Python venv & `ffmpeg`, configures `en_US.UTF-8` locale, hardens permissions, and registers the `systemd` service):

```bash
apt update && apt install -y git
git clone [https://github.com/DENSM0G/x-discord-media-bot.git](https://github.com/DENSM0G/x-discord-media-bot.git) /opt/x-discord-media-bot
cd /opt/x-discord-media-bot
bash scripts/install.sh
```

### 3. Configuration
Edit `/opt/x-discord-media-bot/.env`:

```bash
nano /opt/x-discord-media-bot/.env
```

Key parameters (see `.env.example` for the full list):

```env
DISCORD_TOKEN=your_discord_bot_token_here
PUBLIC_BASE_URL=[https://sub.domain.duckdns.org](https://sub.domain.duckdns.org)
WEB_HOST=0.0.0.0
WEB_PORT=8080
PROXY_URL=socks5://127.0.0.1:1080

MIN_FREE_SPACE_GB=5.0
MAX_POST_DISK_PERCENT=50
MIN_FREE_RAM_MB=512
MAX_CONCURRENT_JOBS=2

ALLOW_DMS=false
ALLOWED_GUILD_IDS=
MAX_COMMENT_LENGTH=50

DEFAULT_VIEW_MODE=carousel
SHOW_STORAGE_STATUS=true
MAX_CONCURRENT_DOWNLOADS_PER_IP=10
TRUSTED_PROXY_IPS=127.0.0.1,::1
RESTRICT_WEB_TO_TRUSTED_PROXIES=false

VIDEO_CODEC=libsvtav1
VIDEO_PRESET=4
```

*(Optional)* For age-restricted posts, export your X browser cookies in Netscape format and place them at `/opt/x-discord-media-bot/config/cookies.txt`, then restrict permissions:

```bash
chown root:xbot /opt/x-discord-media-bot/config/cookies.txt
chmod 640 /opt/x-discord-media-bot/config/cookies.txt
```

### 4. Start & Update
Start the service and follow logs:

```bash
systemctl start x-discord-bot
journalctl -u x-discord-bot -f
```

Update the bot and extractors (`gallery-dl` / `yt-dlp`):

```bash
cd /opt/x-discord-media-bot
bash scripts/update.sh
```

---

## Project Structure

```text
x-discord-media-bot/
├── app/
│   ├── main.py              # Entry point: temp cleanup, disk audit, FastAPI & Discord startup
│   ├── config.py            # Typed configuration loader, IP/CIDR parsing & subprocess env sanitizer
│   ├── bot.py               # Discord client: webhook relay, access control, per-channel locks, FIFO edits
│   ├── extractor.py         # Message filter, gallery-dl/yt-dlp wrapper, Quote Tweets & live disk watcher
│   ├── media_processor.py   # RAM guard, ffprobe, WebP previews, GIF conversion, AV1/VP9/HEVC encoding
│   ├── storage.py           # Post directories, media whitelist, queue.json, disk budget & FIFO cleanup
│   └── web.py               # FastAPI routes, security middleware, per-IP ZIP rate-limit & streaming ZIP
├── web/
│   ├── templates/           # Jinja2 templates (gallery.html, gone.html)
│   └── static/              # CSS & Vanilla JS (4 view modes, Lightbox, live storage footer)
├── scripts/
│   ├── install.sh           # Dependencies, isolated xbot user, UTF-8 locale & hardened systemd unit
│   └── update.sh            # Git pull & extractor updates
├── config/                  # Directory for cookies.txt
├── .env.example             # Configuration template
├── README.md                # English documentation
└── README.ru.md             # Russian documentation
```