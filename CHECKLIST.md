# CHECKLIST — First manual run

This is a single-page step-by-step checklist with the exact UI clicks and terminal commands for your *first* manual run-through. Follow it in order. Keep a terminal/editor and Discord Developer Portal open.

---

## 1) Developer Portal — enable intents & copy token

1. Open **Discord Developer Portal** → click **Applications** in the left column.
2. Click your application (the app for this bot).
3. In the left menu click **Bot**.
4. Under **Build-A-Bot** / **Privileged Gateway Intents** **click to enable**:

   * **Server Members Intent**
   * **Presence Intent**
   * **Message Content Intent**

   > Click **Save Changes** if the portal shows that button.
   > These intents are required by the Python bot to read members/presences/message content. 
5. Still on the **Bot** page: click **Copy** (next to **TOKEN**) and paste into your local `.env` for `BOT_TOKEN`. Keep it secret.

---

## 2) Invite the bot to your server

1. In the Developer Portal left menu click **OAuth2** → **URL Generator**.
2. Under **SCOPES** check **bot** and **applications.commands**.
3. Under **BOT PERMISSIONS** check at minimum:

   * View Channels
   * Send Messages
   * Read Message History
   * Manage Roles
   * Manage Messages
   * (optional) Embed Links, Attach Files
4. Click **Copy** (the generated URL) → open a new browser tab → paste & Enter → pick your server → **Authorize** → complete captcha if shown.

---

## 3) Server: roles & channel prep

1. In Discord, click your **Server Name** → **Server Settings** → **Roles**.
2. Click **Create Role** (plus `+`) → name it `Sus` (or your chosen Sus role name) → **Save Changes**. (Or leave it to the bot — it will create the role automatically. You just need to choose the role name and edit the .env file.)
3. Make sure the bot’s role is **above** the `Sus` role in the role list: drag the bot role above the `Sus` role. This lets the bot add/remove the role.

   * (Server Settings → Roles → drag the bot role)
4. Create or pick a verify channel: click the **+** next to Text Channels → name it `get-verified` (or `verify`) → **Create Channel**.
5. Optional: Channel permissions (if you plan to restrict unverified users): click channel → **Edit Channel** → **Permissions** → configure permissions for `@everyone` and for your unverified role as needed.

---

## 4) Local machine — venv, deps, .env

> **Important:** `cd` into the project directory first (the folder that contains `bot.py`, `register_commands.py`, `requirements.txt`).

```bash
# inside project directory
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env    # or copy manually on Windows, then edit .env
```

Open `.env` in your editor and set at minimum:

* `BOT_TOKEN` (from Developer Portal)
* `CLIENT_ID`
* `GUILD_ID` (your server id)
* `VERIFY_CHANNEL_ID` (verify channel id)
  (`bot.py` reads these at startup). 

---

## 5) Register slash commands

1. In terminal (still in project dir / virtualenv) run:

```bash
python register_commands.py
```

2. Watch the output — it prints existing and the registered commands (success = list of commands). If the commands register, wait a few seconds and reload Discord (Ctrl+R). 
3. **If normal registration is stuck**: run the fallback forcibly **only** as a last resort:

```bash
python register_commands_force.py
```

The force script overwrites guild commands. Use it only when necessary. 

---

## 6) Start the bot
```bash
python bot.py
```

* Check terminal: you should see `Logged in as WebClientDetector#...` and a small debug of intents/app-commands. If the bot exits with missing env errors, re-open `.env` and correct values. 

---

## 7) First in-server checks & clicks (admin user)

1. Confirm bot is online: open Member List (right-side) and look for the bot — status should be online.
2. In your **verify channel** run the slash command:

   * Type `/setupverify` and press Enter **inside the configured verify channel**.
   * Or, if the bot posted an admin prompt message, click the **Configure Verification** button it posted. (If it says “You are not allowed”, make sure you are in `ADMIN_ROLE_IDS` or have admin role.) 
3. When the interactive setup appears:

   * Click the select menu for verification methods → pick method(s) → click **Confirm** → wait for the bot to create the persistent verify message (it will post a message with a **Verify** button).
   * If the bot removes previous messages, that’s expected — it will create the persistent verify message for users. 

---

## 8) Test commands

1. Prefix test (in any channel bot can read): type `!ping` → expect `pong`.
2. Slash test: press `/` and locate these commands: `setupverify`, `setlog`, `verifyuser`, `autoscan`, `scan`. If any are missing, re-run `python register_commands.py` and reload Discord (Ctrl+R). 
3. Scan test (admin-only):

   * Slash: `/scan member:@username` → the bot sends you an ephemeral result (platforms, joined date). If web-only it offers to mark Sus. 
   * Prefix: `!scan <@user-id>` also works and can be used when member selection is limited.

---

## 9) Verify the Sus flow

1. When a user is marked Sus the bot logs to the configured log channel and will post a small non-pinging mention message (bot sends then deletes per config). Confirm log entry in your log channel. 
2. To remove Sus: as admin run `/verifyuser member:@username` or use the verify flow if the user completes the challenge. 

---

## 10) Final sanity checks

* Open the verify channel and confirm the persistent verify message is present and that the **Verify** button is visible.
* Check the bot console for errors; address any missing env var errors. 

---

### Quick troubleshooting notes

* If slash commands are not visible: `python register_commands.py` → wait → reload Discord (Ctrl+R). 
* If presence/platforms show `offline/no-presence`: confirm **Presence Intent** is enabled in Developer Portal and that the bot was restarted after toggling. 
* If the bot can’t add roles: move the bot role above the `Sus` role in Server Settings → Roles.