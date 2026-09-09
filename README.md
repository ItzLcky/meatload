# Discord Bot

A self-hosted Discord bot for a private server: music, custom commands, moderation,
welcome messages, self-assign roles, leveling, and the usual small utilities.

Runs as a single Docker container. No external services, no database server —
state lives in one SQLite file you can copy.

Every command works two ways: as a slash command (`/play`) and with a text
prefix (`!play`). Mentioning the bot also works as a prefix, which is the escape
hatch if someone changes the prefix and forgets it.

---

## Quick start

### 1. Install Docker

Docker isn't installed on this machine yet. On CachyOS / Arch:

```fish
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

Log out and back in (or run `newgrp docker`) so the group change takes effect,
then check it: `docker run --rm hello-world`.

### 2. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_TOKEN`.
3. Still on the **Bot** tab, under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** — welcome messages, autorole, `/userinfo`
   - **Message Content Intent** — prefix commands and custom commands

   The bot will refuse to start without these and tell you so.
4. Invite it with this URL, replacing `YOUR_APP_ID` with the Application ID from
   the **General Information** tab:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=1374846053462&scope=bot%20applications.commands
   ```

   That permission set is everything the bot's features need and nothing more —
   notably not Administrator.

### 3. Configure and run

```fish
cp .env.example .env
$EDITOR .env   # paste DISCORD_TOKEN, set DEV_GUILD_IDS to your server's ID

docker compose up -d --build
docker compose logs -f bot
```

**Check `PUID`/`PGID` in `.env`.** The bot writes its database to `./data` on the
host, so the container has to run as a uid that can write there. Run `id -u` and
`id -g` and put those numbers in `.env` — `1000`/`1000` is the default and is
almost certainly right for a single-user desktop. If it's wrong the bot exits
immediately with a message telling you exactly what to change, rather than
failing obscurely later.

**Set `DEV_GUILD_IDS`.** Slash commands registered to a specific server appear
instantly; global ones take up to an hour. Right-click your server → *Copy
Server ID* (Developer Mode must be on in Discord's Advanced settings).

There's a `Makefile` wrapping the common commands — `make up`, `make logs`,
`make restart`, `make backup`, `make help`.

---

## Running it from a Docker UI (Arcane, Portainer, Dockge…)

Use `docker-compose.arcane.yml` rather than `docker-compose.yml`.

A management UI deploys a compose file *without* the source tree beside it, so
the normal file's `build: context: .` has nothing to build from, and its
`./data` bind mount would resolve against the manager's own stack directory
instead of this project. The Arcane variant fixes both: it runs a pre-built
image and takes absolute host paths.

### 1. Get an image

The UI can't build one. Either let CI publish it (below), or build it once on
the host:

```fish
git clone https://github.com/ItzLcky/meatload.git /opt/meatload
cd /opt/meatload
docker build -t discord-bot:latest .
```

### 2. Create the stack

Make a new stack/project, paste in the contents of `docker-compose.arcane.yml`,
and set these variables in its environment editor:

| Variable | Value |
| --- | --- |
| `DISCORD_TOKEN` | your bot token — the stack refuses to start without it |
| `DEV_GUILD_IDS` | your server ID, so slash commands appear instantly |

That's it. Everything else has a sensible default. There are no host directories
to create and no PUID/PGID to work out: the file uses named volumes, which
Docker creates already owned by the user inside the image.

Deploy, then watch the logs for `Connected as …`.

> Don't change the volume lines to a path like `./data` or `meatload/data`. A
> relative path resolves against the manager's own project directory — which is
> a path inside *its* container, not one the Docker daemon can bind — and a
> source with no leading `/` or `./` is read as a named volume, giving
> `service "bot" refers to undefined volume`. If you do want the database
> directly on the host, use an absolute path you have created and chowned
> yourself, and add `user: "1000:1000"` back to match its owner.

### 4. Sync the slash commands

Only needed if you left `DEV_GUILD_IDS` empty: send `!sync` in any channel.

### Using the image CI publishes (recommended)

`.github/workflows/ci.yml` runs the tests on every push and, when they pass on
`main`, builds the image and publishes it to GitHub Container Registry as
`ghcr.io/itzlcky/meatload:latest`. That removes the SSH-and-build step entirely:
push code, then hit redeploy in Arcane.

Because the repo is private, so is the package, so whatever pulls it has to
authenticate. Create a token at *Settings → Developer settings → Personal access
tokens (classic)* with the **`read:packages`** scope — that single scope is all a
pull needs. Use a **classic** token: fine-grained tokens have had patchy GHCR
support, and it isn't worth debugging. If a pull still 403s, add read-only
`repo`, since the package inherits the private repo's permissions.

**Where that token has to live depends on how Arcane runs.** Registry
credentials come from whatever *asks* the Docker daemon to pull, not from the
daemon itself:

- **Arcane in a container** (the usual case) — a `docker login` you run as
  yourself writes `~/.docker/config.json` on the host, which Arcane can't see.
  Add the registry in Arcane's own settings instead: look for a Registries or
  Registry Credentials section, and add `ghcr.io` with username `ItzLcky` and
  the token as the password.
- **Arcane directly on the host, running as your user** — then this is enough:

  ```fish
  echo <token> | docker login ghcr.io -u ItzLcky --password-stdin
  ```

Then set two more variables on the stack:

| Variable | Value |
| --- | --- |
| `BOT_IMAGE` | `ghcr.io/itzlcky/meatload:latest` |
| `PULL_POLICY` | `always` |

Leaving both unset is what causes `image discord-bot:latest is not available
locally and pull_policy is set to never` — the defaults assume the
build-it-yourself route below.

Don't make the package public to avoid the login — the image contains the bot's
source, so a public image would publish the code your private repo is keeping in.

### Notes for UI deployments

- **Updating the code.** With CI: push to `main`, wait for the run to go green,
  then redeploy the stack in Arcane (`PULL_POLICY=always` makes it fetch the new
  image). Building by hand instead: `git pull`, `docker build -t
  discord-bot:latest .`, then recreate the container. The default
  `pull_policy: never` exists for that second case — a local image was never
  pushed anywhere, so an auto-updating manager would just fail trying to pull
  it.
- **Updating yt-dlp** does *not* need a rebuild. `YTDLP_AUTO_UPDATE` is on by
  default, so restarting the container from Arcane pulls the newest version.
  That's the fix for most YouTube breakage.
- **If it exits immediately**, check the logs. A permissions mismatch prints
  exactly which `PUID`/`PGID` to set rather than failing obscurely.
- **Health** shows in the container list once it's connected — the bot
  refreshes a heartbeat file every 30 seconds.
- **Backups.** With named volumes the database lives inside Docker rather than
  at a host path, so copy it out with
  `docker cp discord-bot:/app/data/bot.db ./bot-backup.db`. `/tag export` in
  Discord covers the custom commands on their own.

I don't have Arcane here to test against, so the exact menu names may differ
from what I've described — but "create a stack, paste compose, set environment
variables, deploy" is the flow in every one of these tools.

---

## Commands

`/help` lists everything in Discord, and `/help <command>` explains one.

### Music

| Command | What it does |
| --- | --- |
| `/play <query or link>` | Play or queue. Accepts search terms, YouTube, SoundCloud, Bandcamp, direct audio links, and Spotify links (see below). |
| `/playnext <query>` | Queue at the front. |
| `/search <query>` | Pick from the top 5 YouTube results. |
| `/queue` `/nowplaying` | See what's queued and playing. |
| `/skip` `/back` `/pause` `/resume` `/stop` | Transport. |
| `/seek 1:30` | Jump to a position. |
| `/volume 80` | 0-200%, remembered per server. |
| `/loop [off\|track\|queue]` | Repeat modes. |
| `/shuffle` `/clear` `/remove <n>` `/move <from> <to>` `/dedupe` | Queue editing. |
| `/playlist save\|load\|delete <name>` | Save the current queue and reload it later. |

The now-playing message carries buttons for pause, skip, stop, loop, shuffle,
and the queue, so most listening needs no typing.

**Playlist links.** A `/playlist?list=...` link queues the whole playlist. A
`watch?v=...&list=...` link queues just that one video — those come from clicking
a video *inside* a playlist, where dumping 200 tracks in is rarely what was meant.

**Spotify.** Spotify doesn't allow bots to stream its audio, so Spotify links are
read for their track names and the audio is found on YouTube. This needs
`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env` (free, from
<https://developer.spotify.com/dashboard>). Without them the bot says so and
suggests searching by name.

**Who can control playback.** By default anyone can. Set a DJ role with
`/config djrole @DJ` and the disruptive commands (skip, stop, volume, seek,
clear) get restricted to it — but only while more than one person is listening.
Alone in a channel, you always control your own music.

### Custom commands

```
/tag create hello Hey {user}, welcome to {server}!
!hello
```

Then `!hello` (or `/tag show hello`) replies. `/tag random` makes one that
answers differently each time:

```
/tag random greet Hi!|Hello there|Yo
```

Variables:

- **Who ran it** — `{user}` mention, `{user.name}` display name,
  `{user.username}`, `{user.tag}`, `{user.id}`, `{user.avatar}`
- **Where** — `{server}`, `{server.id}`, `{server.members}`, `{server.icon}`,
  `{channel}` mention, `{channel.name}`, `{channel.id}`
- **Input** — `{args}` for everything typed after the name, or `{0}`, `{1}`, …
  for individual words
- **Meta** — `{count}`, how many times it's been used

`/tag edit`, `/tag delete`, `/tag alias`, `/tag all`, `/tag search`, `/tag info`,
`/tag raw`, `/tag export`. Creating tags needs Manage Messages, or a role you
nominate with `/config tagrole @Role`. Tags can never ping `@everyone` or a role,
whatever their content says, and they never shadow a real command.

See [Migrating from Red](#migrating-from-red) to bring your existing ones over.

### Moderation

`/kick` `/ban` `/softban` `/unban` `/timeout` `/untimeout` `/warn` `/purge`
`/slowmode` `/lock` `/unlock`

Every action becomes a numbered case in a persistent history. `/warnings @user`
shows someone's record — anyone can check their own, Manage Messages is needed
to see others'. `/reason <case> <text>` corrects a case; `/delcase <case>`
removes one. Point `/config modlog #channel` at a channel to mirror everything
there.

### Welcome, autorole, and self-assign roles

```
/welcome channel #general
/welcome message Welcome {user}, you're member #{count}!
/autorole @Member
```

`/goodbye` configures leave messages the same way. `/welcome test` fires the flow
against yourself so you can see it before anyone joins.

For self-assign roles, `/rolepanel create` posts a panel and `/rolepanel add`
puts role buttons on it:

```
/rolepanel create title:Pick your games max_roles:0
/rolepanel add panel_id:1 role:@Minecraft
```

`max_roles:1` makes it behave like a radio button — picking one swaps out the
other. Buttons are used rather than reactions, and they keep working across bot
restarts.

### Leveling

Off by default. `/leveling toggle on` starts it. Then `/rank`, `/leaderboard`,
and:

```
/leveling announce channel #level-ups     # or `here`, or `off`
/leveling rate 15 25 60                   # 15-25 XP per message, once a minute
/leveling reward 10 @Regular              # role at level 10
/leveling ignore #spam
```

`/leveling setxp` and `/leveling reset` fix things up. Level rewards stack —
reaching level 10 also grants any level 5 reward that was missed.

### Fun and utility

`/poll` (live vote bars), `/8ball`, `/roll 2d20+3`, `/coinflip`, `/choose`,
`/say`, `/remindme 2h feed the cat`, `/reminders`, `/ping`, `/botinfo`,
`/avatar`, `/userinfo`, `/serverinfo`, `/roleinfo`.

Reminders live in the database, so they survive restarts.

### Server settings

`/config` shows everything below at a glance.

| Command | What it does |
| --- | --- |
| `/config prefix ?` | Change the text prefix. Mentioning the bot always works too. |
| `/config modlog #channel` | Mirror moderation actions into a channel. |
| `/config djrole @DJ` | Restrict disruptive music commands. |
| `/config tagrole @Role` | Let a role manage custom commands. |
| `/config cleanup on` | Delete command messages after they run. |

**Command cleanup.** Off by default, and Manage Server to change. With
`/config cleanup on`, the bot deletes the message that ran a text command once
the command has answered, so a busy channel keeps the replies instead of both
halves of every exchange. Custom commands (tags) are cleaned up too.

Three things it deliberately leaves alone: slash commands, which never post a
message to delete; commands that *failed*, so the error still has the mistyped
command above it to point at; and anything at all if the bot is missing **Manage
Messages** — `/config cleanup on` says so at the time if that permission isn't
there yet.

### Owner-only

Maintenance is text-only, since `sync` is what creates the slash commands:

- `!sync` — push slash commands to the current server. `!sync global` for
  everywhere, `!sync clear` to wipe this server's.
- `!reload music` — reload a cog after editing code, no restart.
- `!shutdown` — clean exit. Docker's `restart: unless-stopped` brings it back.

`status` changes what the bot itself is showing, and works either way:

```
/status set watching       the queue     # Playing / Listening to / Watching /
/status set custom         back at 6pm   #   Competing in / plain custom text
/status streaming https://twitch.tv/you  going live
/status presence dnd                     # online | idle | dnd | invisible
/status clear                            # drop the text, keep the dot
/status show                             # what's showing right now
```

Whatever you set is saved and re-applied on the next start, so it outlives a
restart or a `docker compose up -d`. `ACTIVITY_NAME` / `ACTIVITY_TYPE` in `.env`
are only the starting point, used until the first `status` command.

Owners come from `OWNER_IDS` in `.env`, or default to the application owner.

---

## Migrating from Red

`/tag import` brings your Red custom commands across. Upload Red's data file,
check the preview, confirm.

### 1. Find Red's custom commands file

It's at `<red data dir>/cogs/CustomCommands/settings.json`. On a normal install:

```fish
~/.local/share/Red-DiscordBot/data/<instance>/cogs/CustomCommands/settings.json
```

Not sure of the path? Red tells you — run `[p]datapath` in Discord, or
`redbot --list` in a shell to see your instance names. If Red runs in Docker,
copy it out with `docker cp <red-container>:/data/cogs/CustomCommands/settings.json .`

If Red is set to a Postgres or Mongo backend there's no such file. Convert it
first with `redbot-setup convert <instance> json`, then use the file that
produces.

### 2. Import it

In Discord, run `/tag import` and attach the file. You'll get a preview showing
exactly what will be created, what's being renamed, and what's being skipped —
**nothing is written until you press the button.**

```
/tag import file:settings.json
/tag import file:settings.json overwrite:True          # replace existing tags
/tag import file:settings.json source_server:133049272517001216
```

`source_server` is only needed when the file covers more than one server; the
error message lists the IDs it found. Importing needs Manage Server.

There's also a CLI, if you'd rather not upload anything — it previews by default
and only writes with `--apply`:

```fish
make venv
PYTHONPATH=. .venv/bin/python -m bot.tools.import_red \
    ~/.local/share/Red-DiscordBot/data/myinstance \
    --guild-id 133049272517001216 --apply
```

It accepts the `settings.json`, Red's data directory, or just an instance name
(which it resolves through Red's own config).

### What carries over

Placeholders are rewritten automatically:

| Red | Here | |
| --- | --- | --- |
| `{author}` | `{user.tag}` | |
| `{author.mention}` | `{user}` | |
| `{author.name}` | `{user.username}` | |
| `{author.display_name}`, `{author.nick}` | `{user.name}` | |
| `{author.id}` | `{user.id}` | |
| `{server}`, `{guild}`, `{server.name}` | `{server}` | |
| `{server.id}` | `{server.id}` | |
| `{server.member_count}` | `{server.members}` | |
| `{channel}` | `{channel.name}` | Red's bare `{channel}` is the **name**, not a mention |
| `{channel.mention}` | `{channel}` | |
| `{0}`, `{1}`, … | unchanged | |

Multi-response commands (Red's `[p]cc create random`) stay random. Names get
lowercased and cleaned up — `Server Rules` becomes `server-rules` — and the
preview tells you which were renamed.

**What doesn't carry over**, and is listed explicitly in the preview rather than
being silently dropped:

- **Per-command cooldowns.** Red supports them; this bot doesn't.
- **Arbitrary attribute placeholders** like `{author.top_role}` or
  `{message.jump_url}`. Red allowed any attribute; here the supported set is
  fixed, so anything outside it is left as literal text for you to edit. The
  preview names the commands affected.
- **Commands whose name collides with a built-in.** A Red command called `play`
  would be permanently shadowed by the music command, so it's skipped rather
  than imported into something that could never run. Rename it and re-import, or
  re-create it under a different name.
- **Red's `Alias` cog.** Those alias *bot commands* rather than text responses,
  so there's nothing to translate. Most have equivalents here already — check
  `/help`.

Run the import twice and nothing is duplicated: existing names are left alone
unless you pass `overwrite: True`.

### Backups

`/tag export` downloads every custom command here as JSON, and `/tag import`
accepts that file back — so it doubles as a backup and a way to copy commands
between servers. Aliases and random responses survive the round trip.

---

## When music breaks

This is the thing that breaks, because YouTube actively fights extraction. In
rough order of likelihood:

**Everything suddenly fails to play.** yt-dlp is out of date. It ships fixes
within hours of a YouTube change:

```fish
docker compose restart bot     # with YTDLP_AUTO_UPDATE=true, this is the fix
```

`YTDLP_AUTO_UPDATE=true` (the default in `.env.example`) pulls the newest yt-dlp
on every container start, which is why a restart is usually enough. To bake it
into the image instead: `docker compose build --pull && docker compose up -d`.

**"Sign in to confirm you're not a bot."** YouTube wants a session. Export
cookies from a browser logged into a throwaway Google account using a
Netscape-format cookie extension, save as `cookies/cookies.txt`, and set:

```
YTDLP_COOKIES_FILE=/app/cookies/cookies.txt
```

Then `docker compose restart bot`. This also unlocks age-restricted videos.

**Still failing after an update.** Occasionally the fix is a yt-dlp extractor
argument before it becomes the default. It's exposed without needing a rebuild:

```
YTDLP_EXTRACTOR_ARGS=youtube:player_client=default,-web_creator
```

**Audio is choppy.** Check `docker stats`. FFmpeg transcodes to PCM so live
volume control works; that's a fraction of one core per stream, which is nothing
on 24 cores, but a CPU-starved host will stutter.

**The bot joins and says nothing.** Almost always a permission problem — it
needs Connect *and* Speak in that specific channel, which channel-level
overwrites can remove even when the server-level role has them.

---

## Development

```fish
make venv     # Python 3.12 venv via uv, matching the container
make test     # 149 tests, no network or token needed
```

The same suite runs in CI on every push and pull request, and a failing run
blocks the image from being published.

The suite covers the queue and player logic (loop modes, skip, history, the
playback clock), the XP curve, template rendering, the database layer and its
migrations, and — most usefully — loading every cog and converting the whole
slash-command tree to the payload Discord receives on sync. That last one
catches broken decorators and bad annotations without needing a token.

Python 3.12 is deliberate: 3.13 removed `audioop` from the standard library,
which discord.py's voice and volume handling depends on.

```
bot/
  __main__.py     entrypoint
  bot.py          Bot subclass: prefixes, extension loading, error handling
  config.py       environment -> frozen Config
  db.py           SQLite access and migrations
  migrations/     numbered .sql files, applied once each, tracked in-database
  redimport.py    Red custom-command parsing and placeholder translation
  tools/          command-line utilities (`python -m bot.tools.import_red`)
  music/
    source.py     yt-dlp and Spotify -> Track objects
    queue.py      the per-guild queue
    player.py     one asyncio task drives one voice connection
    views.py      now-playing buttons
  cogs/           one module per feature area
  utils/          embeds, checks, formatting, pagination, presence
tests/            stdlib unittest, no dev dependencies
```

To add a feature, drop a cog in `bot/cogs/` and add it to `EXTENSIONS` in
`bot/bot.py`. To change the schema, add `bot/migrations/002_whatever.sql` —
migrations run automatically at startup and are applied exactly once.

While iterating you can edit a cog and run `!reload <cog>` in Discord instead of
restarting, as long as the file is mounted or copied in.

---

## Backups and data

Everything is in `data/bot.db`. (`data/.python` also appears, holding the
self-updated yt-dlp — it's a cache, not data, and is safe to delete.) The bot is
the only writer, so a copy is a valid backup:

```fish
make backup                    # -> data/bot-20260908-143000.db
```

To move the bot to another machine, copy `.env` and `data/` and run
`docker compose up -d --build`. Nothing else is stateful.

The container runs as a non-root user, exposes no ports (Discord connections are
outbound only), and logs are capped at 5 x 10 MB so they can't fill the disk.
`docker compose ps` shows a health status, driven by a heartbeat the bot
refreshes every 30 seconds while connected.
