# Discord One-Time Code Bot

A small Discord bot that distributes one-time codes without issuing the same
code twice. One bot can serve multiple Discord servers; each server has its own
role setting, code pool, statistics, and history.

## Commands

- `/setup role:@Role` — select the role that can use the bot (owner/admin only)
- `/addcodes codes:...` — add space-, comma-, or newline-separated codes
- `/getcodes amount:5` — privately claim codes
- `/available` — see the unused count
- `/stats` — see total, unused, and claimed counts
- `/history` — see recent claim activity without revealing the codes

The user specified by `BOT_OWNER_ID` bypasses the role requirement. Server
administrators can run `/setup`, but do not automatically receive access to the
other commands.

## Discord setup

1. Create an application at <https://discord.com/developers/applications>.
2. Open **Bot**, create the bot, and reset/copy its token.
3. Do not enable privileged intents; this bot does not require them.
4. Under **OAuth2 > URL Generator**, select the `bot` and
   `applications.commands` scopes.
5. Give it **View Channels** and **Send Messages**, then use the generated URL
   to invite it to both servers.
6. Enable Discord Developer Mode, right-click your account, choose **Copy User
   ID**, and use that value for `BOT_OWNER_ID`.

Never share or commit the bot token.

## Run locally

Python 3.10 or newer is required.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Edit `.env` before starting:

```env
DISCORD_TOKEN=your_real_bot_token
BOT_OWNER_ID=your_numeric_discord_user_id
DATABASE_PATH=codes.db
```

The program creates the SQLite database and all tables automatically.

## Railway

1. Upload this project to a private GitHub repository and deploy it on Railway.
2. In the Railway service, add a persistent volume mounted at `/data`.
3. Add these service variables:

```env
DISCORD_TOKEN=your_real_bot_token
BOT_OWNER_ID=your_numeric_discord_user_id
DATABASE_PATH=/data/codes.db
```

4. Deploy. `railway.json` starts the worker with `python main.py`.
5. After the bot appears online, run `/setup role:@YourRole` once in each
   Discord server.
6. Enable Railway volume backups when you move from testing to real codes.

Do not delete or wipe the Railway volume. Do not commit a local `codes.db` file.

## Behavior and safety

- Codes are unique inside each Discord server.
- Claims use an immediate SQLite transaction, so simultaneous requests cannot
  receive the same code.
- If fewer codes remain than requested, no codes are claimed.
- Bot replies are ephemeral, including code delivery and history.
- `/history` records the user, quantity, time, and claim ID. Exact claimed codes
  stay in the database but are not displayed by `/history`.
- The maximum claim is 50 codes at once; the maximum history view is 25 claims.
