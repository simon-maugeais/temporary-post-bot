# Temporary Forum Post Bot

A Python 3.12+ Discord bot that manages temporary posts created through Discord's
normal Forum **New Post** interface. Users keep posting text, pictures, GIFs,
videos, attachments, and replies as usual. The bot never copies or modifies
their content and requires no post-creation command.

## How it works

1. A user publishes a post in a configured Forum channel.
2. The bot replies inside it: **How long should this post remain available?**
3. The author chooses **5 minutes**, **1 hour**, **6 hours**, **12 hours**, **24 hours**,
   **3 days**, or **7 days** using buttons. Other users receive a private refusal.
4. SQLite saves the expiration before the bot replaces its prompt with a
   confirmation using Discord's local-time and relative-time timestamps.
5. A background worker checks every 30 seconds and deletes the entire thread
   when its expiration arrives, including its starter post, attachments, and replies.

All lifetimes are measured **from the original post creation time**, including
the durations selected with buttons. By default, an author has one hour to
choose; after that, the post receives a 24-hour lifetime from its creation time.
Choosing a short lifetime late can therefore make a post expire immediately.
Deletion is permanent; Discord's ordinary auto-archive feature is separate from
deletion and does not cancel the schedule.

## Requirements

- Python **3.12 or newer** and pip.
- A Discord server with one or more **Forum** channels and permission to install
  a bot. Enable Community features if Discord requires them to create Forums.
- A Discord bot token, Internet access, and a writable local database directory.
- A running computer or server. SQLite survives a stopped bot, but deletion
  happens only while the bot is running and can contact Discord.

The tested Discord library is pinned to **discord.py 2.7.1**. Other runtime
dependencies are `aiosqlite` and `python-dotenv`; no voice dependencies are needed.

## Create the Discord bot and obtain its token

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**, give it a name, and open the application.
3. Open **Bot**. If necessary, click **Add Bot**.
4. Use **Reset Token** to generate a bot token, then copy it into your local
   `.env` file as `DISCORD_TOKEN`. Do not use the application ID, public key, or
   OAuth client secret as the token. Keep the token private; reset it if exposed.
5. Leave all **Privileged Gateway Intents** switches off (see below).
6. Do not configure an **Interactions Endpoint URL**. This bot receives button
   interactions over its Gateway connection.

Discord's [application setup guide](https://docs.discord.com/developers/quick-start/getting-started)
provides additional portal instructions. This project needs a server installation.

## Invite the bot and grant permissions

Under **OAuth2 → URL Generator**, select the **bot** scope and the four permissions
listed below. Open the generated URL, choose your server, and authorize it using
an account with **Manage Server**. If using the **Installation** page instead,
enable **Guild Install** and configure the same bot scope and permissions.

For a direct invite, substitute your application's ID into:

```text
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot&permissions=292057842688
```

The bot does not register slash commands; `applications.commands` is not required
for this workflow. Do **not** grant Administrator.

### Exact bot permissions

| Permission | API name | Purpose |
| --- | --- | --- |
| View Channel | `VIEW_CHANNEL` | See the configured Forum and its threads. |
| Read Message History | `READ_MESSAGE_HISTORY` | Fetch the starter when needed and recover/edit the bot's prompt. |
| Send Messages in Threads | `SEND_MESSAGES_IN_THREADS` | Send prompts and confirmations inside Forum posts. |
| Manage Threads | `MANAGE_THREADS` | Delete complete threads and reopen archived threads when a notice needs updating. |

Forum settings may label **Send Messages in Threads** as **Send Messages in Posts**
and **Manage Threads** as **Manage Posts**. Apply these permissions to the bot's
role in every configured Forum; category/channel overrides can deny permissions
granted by the invite. Prefer granting access and management only in those Forums.

The bot does not need **Send Messages / Create Posts** in the parent Forum,
**Create Public Threads**, **Manage Messages**, **Attach Files**, **Embed Links**,
**Mention Everyone**, or **Administrator**. Buttons have no additional bot
permission. Users still need the server's usual permission to view/interact with
the post. Thread permissions are inherited from the Forum, and sending in a
thread uses `SEND_MESSAGES_IN_THREADS` rather than `SEND_MESSAGES`, as documented
in Discord's [thread permissions](https://docs.discord.com/developers/topics/threads#permissions).

### Exact Gateway intents

Only **GUILDS** (`discord.Intents.guilds = True`) is enabled. This is a standard,
non-privileged intent set in code; there is no Developer Portal switch to enable it.

Leave **Server Members Intent**, **Presence Intent**, and **Message Content Intent**
off. Message, reaction, and typing intents are also disabled. The bot uses thread
metadata and REST requests for author identity, not message content or member lists.
The bot's own messages and components remain accessible without Message Content.

## Find your Forum channel IDs

1. In Discord, open **User Settings → Advanced → Developer Mode**.
2. Right-click a **Forum channel** in the server's channel list.
3. Choose **Copy Channel ID**.
4. Repeat for additional Forums and put the IDs in a comma-separated list.

Use the parent Forum channel's ID, not an individual post/thread ID. Text channels
and Media channels are not managed by this implementation.

## Install and configure

Run commands from the project directory. Create a virtual environment and install
dependencies before launching the bot.

### Linux / macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Debian/Ubuntu, a missing `ensurepip` usually means the matching
`python3.12-venv` package must be installed first. On systems where `python3`
already selects Python 3.12+, it can replace `python3.12`.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Using the environment's Python directly avoids PowerShell activation policy
issues. Optional activation: `.\.venv\Scripts\Activate.ps1`.

### Windows Command Prompt

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

### Edit `.env`

```dotenv
DISCORD_TOKEN=YOUR_BOT_TOKEN
FORUM_CHANNEL_IDS=123456789012345678,234567890123456789
DATABASE_PATH=data/posts.sqlite3
SELECTION_TIMEOUT_HOURS=1
DEFAULT_POST_LIFETIME_HOURS=24
EXPIRATION_CHECK_INTERVAL_SECONDS=30
LOG_LEVEL=INFO
```

The token must be your own. The example IDs are placeholders. `.env` is ignored
by Git. Existing environment variables take priority over values in `.env`.
The bot always loads `.env` beside `config.py`; relative database paths are also
resolved against the project directory, regardless of your working directory.
Parent directories for the database are created automatically.

The three timing settings can alternatively be changed through their constants
in `config.py` when the corresponding environment variable is absent. Positive
fractional hours are accepted. Use a 30–60 second worker interval in normal use.

## Start the bot

Linux/macOS, with the virtual environment activated:

```bash
python bot.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe bot.py
```

Windows Command Prompt:

```bat
.venv\Scripts\python.exe bot.py
```

Wait for **Bot ready** and **Thread recovery completed** in the log, then create
a **new** Forum post through Discord's normal UI. Use Ctrl+C to stop cleanly.
For unattended operation, run under your server's service manager or Windows
Task Scheduler with restart-on-failure enabled and the project directory as its
working directory. Run **one bot process per token/database**.

## Persistence, recovery, and failures

`database.py` creates the `posts` table and indexes automatically. It stores:

- `thread_id`, `guild_id`, `forum_channel_id`, `author_id`, and UTC Unix `created_at`.
- `expires_at` and status: `pending_author`, `awaiting_selection`, `scheduled`,
  `deleted`, or `ignored` (bot-created posts).
- The selection deadline, default expiration, whether the default was applied,
  the bot message ID, a pending message-update flag, and deletion time.

SQLite uses parameterized statements, WAL journaling, and full synchronous
writes. Selection is a conditional database update; only the original author can
change `awaiting_selection` to `scheduled` before the deadline. Per-thread locks
serialize button clicks, automatic defaults, notices, and deletion. A second
click cannot overwrite an expiration, including after a restart.

Persistent views use stable component IDs and `timeout=None`, with a fallback
handler registered at startup. The database validates the author, guild, thread,
and prompt message for every click. Pending prompts are refreshed after restart,
and saved confirmations are retried if the Discord edit failed. The lifetime is
saved **before** the public notice changes. A crash after sending a prompt but
before saving its message ID is recovered by finding the bot's earlier notice
in thread history instead of blindly posting another one.

Every worker cycle applies overdue defaults, deletes expired threads, and repairs
pending author lookups/messages. Permission failures, network errors, and Discord
API errors leave schedules pending for a later attempt. A confirmed **Unknown
Channel** response or thread-delete event marks a thread deleted; a missing bot
message is replaced without forgetting the thread's expiration. Archived threads
are fetched by ID and deleted normally. If a prompt/confirmation needs updating
in an archived thread, the bot reopens it to write its notice.

On startup and reconnect, active threads and recently archived threads are checked
for posts created **since this process started**. Cached threads are also checked
each cycle. This recovers missed events during a temporary disconnection without
adopting historical posts. Posts already in SQLite remain managed across all
restarts, including immediately overdue deletions. Untracked posts created while
the bot process was completely stopped are deliberately ignored when it restarts,
as are all other pre-start posts absent from SQLite.

Deletion normally occurs within one worker interval of expiration; downtime,
API rate limits, or errors can delay it. UTC is used internally; `<t:...:F>` and
`<t:...:R>` render in each reader's locale/timezone. Keep the host clock synchronized.

Keep the database on persistent local storage. Stop the bot before copying the
database for a simple backup, or use SQLite's backup API for a live backup; copying
only the main file while WAL is active can lose recent writes. Do not delete the
database to restart the bot. Removing a Forum from `FORUM_CHANNEL_IDS` pauses its
saved schedules; adding it again resumes them, potentially deleting overdue posts.
Completed/ignored records are retained for audit; no automatic retention cleanup
is implemented.

## Change durations and default behavior

Edit `DURATION_OPTIONS` in `config.py`, then restart:

```python
DurationOption("2 hours", 2 * 60 * 60),
DurationOption("14 days", 14 * 24 * 60 * 60),
```

Provide 1–25 options with unique positive integer seconds and labels of at most
80 characters. Buttons automatically wrap across Discord's five rows. IDs are
based on duration seconds, so renaming a label preserves its meaning. Pending
prompts are refreshed with the new choices at startup. Previously saved
expirations are unchanged.

Change `SELECTION_TIMEOUT_HOURS` and `DEFAULT_POST_LIFETIME_HOURS` in `.env` (or
their defaults in `config.py`) and restart to change the default behavior. Both
values are captured for each post at detection time, so configuration changes
do not silently alter existing deadlines or fallback expirations. A fallback
lifetime shorter than the selection timeout causes deletion as soon as the
default is applied. The initial policy always applies a finite default; there
is no permanent-post option.

## Project layout and extension points

```text
bot.py                  Client lifecycle, Gateway events, logging, startup
config.py               Validated environment settings and duration choices
database.py             SQLite schema, records, atomic state transitions
views.py                Persistent View/Button definitions and UI errors
services/posts.py       Detection, author checks, notices, scheduling, deletion
services/expiration.py  Periodic worker and reconnect discovery
tests/                  Offline async tests with SQLite and mocked Discord
requirements.txt        Runtime dependencies
.env.example            Configuration template
```

Keep future policies in the service layer and add conditional database
transitions for lifetime extensions, moderator overrides, immediate deletion,
per-Forum choices/defaults, permanent posts, and pre-deletion warnings. Slash
commands can call those same services later without changing native post creation.
Those optional features are not implemented here.

## Validation and a live smoke test

Run the offline tests (no bot token or extra test dependency required):

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py config.py database.py views.py services tests
```

On Windows, replace `python` with `.venv\Scripts\python.exe` (or
`.\.venv\Scripts\python.exe` in PowerShell).

For a live test in a disposable Forum, temporarily add a `DurationOption("1 minute", 60)`
and use a short timeout/default, such as `SELECTION_TIMEOUT_HOURS=0.01` and
`DEFAULT_POST_LIFETIME_HOURS=0.03`. Create a post with an attachment, test another
user's click, choose the short option as its author, restart the bot, and verify
the whole thread disappears. Also test a post with no choice and manually delete
a scheduled post. Restore production settings afterwards. Offline tests cannot
verify your server's permission overrides or a real Discord Gateway connection.

## Verified Discord API behavior

The implementation was checked against the released discord.py 2.7.1 source and
Discord's current documentation:

- [`on_thread_create`](https://discordpy.readthedocs.io/en/stable/api.html#discord.on_thread_create)
  receives newly created threads with the GUILDS intent. The library's
  [`parse_thread_create`](https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/state.py)
  distinguishes `newly_created` events from joins/synchronization.
- [`Thread.owner_id`, `starter_message`, and `fetch_message`](https://discordpy.readthedocs.io/en/stable/api.html#discord.Thread)
  support identifying the creator without a Members intent. The creator's user
  object is fetched if absent from cache, allowing bot authors to be ignored.
  If owner data cannot resolve the author, the starter message is fetched using
  the thread ID, which is also the Forum starter-message ID.
- [`Thread.delete`](https://discordpy.readthedocs.io/en/stable/api.html#discord.Thread.delete)
  deletes the thread using the channel-delete API, requiring Manage Threads.
- [Persistent views](https://discordpy.readthedocs.io/en/stable/api.html#discord.Client.add_view)
  register button handlers across restarts when given stable custom IDs and no
  timeout.
