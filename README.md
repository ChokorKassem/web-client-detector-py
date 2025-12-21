
# Web Client Detector (Python)

A small Discord moderation bot that detects users who appear to be **web-only** clients and helps route them through a verification flow or mark them as *Sus* (suspicious). This repository contains the Python implementation — run `bot.py` to start the bot. 

If you want the original JavaScript version, see the JS repo: [Web-Client Detector (JavaScript)](https://github.com/ChokorKassem/web-client-detector-js)

---

# Files & where to put them

- [bot.py](./bot.py) — main bot (drop-in; run this to start the bot).
- [register_commands.py](./register_commands.py) — registers guild slash commands (run after you edit commands).
- [register_commands_force.py](./register_commands_force.py) — fallback that force-replaces guild commands if normal registration fails (use only when needed).
- [requirements.txt](./requirements.txt) — Python dependencies (install with `pip install -r requirements.txt`).
- [.env.example](./.env.example) — example env file (copy to `.env` and fill secrets/IDs).  
- `config.json` — created automatically on first run; stores runtime settings.

---

# Quick start (important: be in the project directory)
Open a terminal and `cd` to the folder containing `bot.py`, `register_commands.py`, and `requirements.txt`. Run these commands from that directory.

1. Create & activate a Python virtual environment, then install dependencies:
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
````

2. Create your `.env` from the example:

```bash
cp .env.example .env   # or copy the file manually on Windows
```

Open `.env` and set at minimum: `BOT_TOKEN`, `CLIENT_ID`, `GUILD_ID`. `bot.py` reads other optional settings such as `VERIFY_CHANNEL_ID`, `SUS_LOG_CHANNEL_ID`, `ADMIN_ROLE_IDS`, and `COMMAND_PREFIX`. 

3. Register slash commands (run once, or whenever you change commands):

```bash
python register_commands.py
```

If registration fails repeatedly, you can use `register_commands_force.py` as a last-resort replacement to overwrite guild commands — only use it if you understand the consequences.

4. Start the bot:

```bash
python bot.py
```

You should see `Logged in as ...` and some debug output. If the bot exits with a message about missing env vars, re-check `.env`. 

---

# Required Discord settings & permissions

* In the **Discord Developer Portal** for the application: enable **Server Members Intent**, **Presence Intent**, and **Message Content Intent** (the bot relies on presence and message-content info to detect platforms and handle prefix commands.). 
* When inviting the bot, include scopes: `bot` and `applications.commands`.
* Recommended bot permissions (minimum for full behavior):

  * View Channels, Send Messages, Read Message History
  * Manage Roles (to add/remove the Sus role)
  * Manage Messages (bot deletes its own notifier messages)
  * Use Application Commands
    For initial testing you can grant Administrator and then restrict permissions back down.

---

# How to use?

1. Confirm the bot is online — check the terminal for `Logged in as WebClientDetector#...`. 
2. Try a simple prefix check (in a channel the bot can read):

   * `!ping` → bot replies `pong`.
3. Configure verification (admin):

   * Run `/setupverify` *inside* the configured verify channel (or click the “Configure Verification” button posted by the bot). Follow the interactive prompts to choose verification methods. 
4. Scan a user (admin):

   * Slash: `/scan member:@username` — shows that user’s platform(s) (ephemeral to the invoker).
   * Prefix: `!scan <@user-id>` — also works if slash mention is limited in private channels. If the user is web-only, the bot prompts to mark them Sus. When confirmed, the bot queues the role change and logs it. 
5. Verify / remove Sus (admin):

   * Slash: `/verifyuser member:@username` — removes Sus role and logs the action. 

---

# Small troubleshooting tips

* **Slash commands don’t appear:** run `python register_commands.py`, wait a few seconds, and reload Discord (Ctrl+R). If the script reports 401/403, re-check `BOT_TOKEN`, `CLIENT_ID`, and `GUILD_ID`. If normal registration keeps failing, `register_commands_force.py` can force-overwrite commands.
* **Presence shows offline/no-presence:** confirm Presence Intent is enabled in Developer Portal and that the user’s presence has had a few seconds to populate after joining. 
* **Bot can’t assign roles:** make sure the bot’s role sits above the Sus role in the server role order and that it has **Manage Roles**.

---

# Short FAQ

Q: Where do I run these commands?
A: From a terminal with your working directory set to the project root (the folder that contains `bot.py`). Do not run them from another folder.

Q: Do I need to run the register script every time?
A: Only when you change command definitions. Otherwise run it once after deployment. 

Q: What is `register_commands_force.py`?
A: A helper that bulk-overwrites guild commands via the REST API; use it only if normal registration fails or commands get stuck. 
