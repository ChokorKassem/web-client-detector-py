# register_commands.py

from __future__ import annotations
import os, sys, json
from dotenv import load_dotenv
import requests

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
GUILD_ID = os.getenv("GUILD_ID")

if not BOT_TOKEN or not CLIENT_ID or not GUILD_ID:
    print("ERROR: BOT_TOKEN, CLIENT_ID and GUILD_ID must be set in your .env")
    sys.exit(1)

API_BASE = f"https://discord.com/api/v10/applications/{CLIENT_ID}/guilds/{GUILD_ID}/commands"
HEADERS = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type": "application/json"
}

# The commands to register (guild-scoped)
COMMANDS = [
    {
        "name": "setupverify",
        "description": "Interactive setup for verification (run in the verify channel)",
    },
    {
        "name": "setlog",
        "description": "Set the channel where verification & sus logs should be sent.",
        "options": [
            {
                "name": "channel",
                "description": "Text channel to use as logs",
                "type": 7,   # CHANNEL
                "required": True
            }
        ]
    },
    {
        "name": "verifyuser",
        "description": "Manually verify (remove Sus role) from a user.",
        "options": [
            {
                "name": "member",
                "description": "Member to verify",
                "type": 6,   # USER
                "required": True
            }
        ]
    },
    {
        "name": "autoscan",
        "description": "Enable or disable automatic daily scanning.",
        "options": [
            {
                "name": "action",
                "description": "on or off",
                "type": 3,  # STRING
                "required": True,
                "choices": [
                    {"name": "on", "value": "on"},
                    {"name": "off", "value": "off"}
                ]
            }
        ]
    },
    {
        "name": "scan",
        "description": "Scan members for platform usage. Optionally restrict them as Sus after confirmation.",
        "options": [
            {
                "name": "member",
                "description": "Check one member only",
                "type": 6,   # USER
                "required": False
            },
            {
                "name": "duration",
                "description": "Quick filter by join time",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "last_hour", "value": "last_hour"},
                    {"name": "last_day", "value": "last_day"},
                    {"name": "last_week", "value": "last_week"},
                    {"name": "last_month", "value": "last_month"}
                ]
            },
            {
                "name": "start",
                "description": "Start ISO timestamp",
                "type": 3,
                "required": False
            },
            {
                "name": "end",
                "description": "End ISO timestamp",
                "type": 3,
                "required": False
            },
            {
                "name": "apply_sus",
                "description": "If true, ask to mark matched users Sus",
                "type": 5,  # BOOLEAN
                "required": False
            }
        ]
    }
]

def show_existing():
    r = requests.get(API_BASE, headers=HEADERS)
    if r.status_code == 200:
        cmds = r.json()
        if not cmds:
            print("No guild application commands currently registered.")
        else:
            print("Existing guild commands:")
            for c in cmds:
                print(f" - {c.get('name')} (id: {c.get('id')})")
    else:
        print("Failed to fetch existing commands:", r.status_code, r.text)

def register_all():
    print("Registering / overwriting guild commands (bulk PUT)...")
    r = requests.put(API_BASE, headers=HEADERS, json=COMMANDS)
    if r.status_code in (200, 201):
        print("Success. Registered commands:")
        for c in r.json():
            print(f" - {c.get('name')} (id: {c.get('id')})")
    else:
        print("Failed to register commands:", r.status_code, r.text)
        sys.exit(1)

if __name__ == "__main__":
    print("== SHOW EXISTING ==")
    show_existing()
    print("\nThis script will now replace guild commands with the commands defined here.")
    register_all()
    print("\nDone. Slash commands should appear in your server shortly (usually instantly).")
