# bot.py
# Web Client Detector

import os
import json
import csv
import asyncio
import secrets
import random
import datetime
import re
import traceback
from pathlib import Path
from typing import Dict, Any, List, Set
import aiocron
import pytz
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
GUILD_ID = int(os.getenv("GUILD_ID") or 0)

VERIFY_CHANNEL_ID = int(os.getenv("VERIFY_CHANNEL_ID") or 0)
SUS_CHAT_CHANNEL_ID = int(os.getenv("SUS_CHAT_CHANNEL_ID") or 0)
SUS_LOG_CHANNEL_ID = int(os.getenv("SUS_LOG_CHANNEL_ID") or 0)

SUS_ROLE_NAME = os.getenv("SUS_ROLE_NAME", "Sus")
ADMIN_ROLE_IDS_RAW = json.loads(os.getenv("ADMIN_ROLE_IDS", "[]") or "[]")
PROCESS_DELAY_MS = int(os.getenv("PROCESS_DELAY_MS", "800"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

# Normalize admin role ids to ints safely
ADMIN_ROLE_IDS: List[int] = []
for rid in ADMIN_ROLE_IDS_RAW:
    try:
        ADMIN_ROLE_IDS.append(int(rid))
    except Exception:
        pass
ADMIN_ROLE_IDS_SET: Set[int] = set(ADMIN_ROLE_IDS)

CONFIG_PATH = Path("config.json")
SUS_PLATFORM_CACHE_PATH = Path("sus_platforms.json")
DEFAULT_CONFIG = {
    "sus_role_id": None,
    "verify_message_id": None,
    "admin_prompt_message_id": None,
    "verification_methods": ["button"],
    "autoscan_enabled": False,
    "log_channel_id": SUS_LOG_CHANNEL_ID or None,
    "periodic_notify_enabled": True,
    "periodic_notify_cron": "0,30 * * * *",
    "periodic_mention_delete_seconds": 30,
    "process_delay_ms": PROCESS_DELAY_MS
}

if not BOT_TOKEN or not GUILD_ID:
    print("ERROR: BOT_TOKEN and GUILD_ID must be set in .env")
    raise SystemExit(1)

intents = discord.Intents.default()
intents.members = True
intents.presences = True   # make sure this is enabled in Dev Portal
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

config: Dict[str, Any] = {}
role_queue: asyncio.Queue = asyncio.Queue()
role_worker_task: asyncio.Task = None
challenge_store: Dict[str, Dict[str, Any]] = {}

sus_platform_cache: Dict[str, Dict[str, Any]] = {}

# -------------------------
# Config & platform-cache helpers
# -------------------------
def load_config():
    global config
    try:
        if CONFIG_PATH.exists():
            config = json.loads(CONFIG_PATH.read_text())
        else:
            config = DEFAULT_CONFIG.copy()
            save_config()
    except Exception:
        config = DEFAULT_CONFIG.copy()
        save_config()

def save_config():
    CONFIG_PATH.write_text(json.dumps(config, indent=2))

def load_sus_platform_cache():
    global sus_platform_cache
    if SUS_PLATFORM_CACHE_PATH.exists():
        try:
            sus_platform_cache = json.loads(SUS_PLATFORM_CACHE_PATH.read_text())
        except Exception:
            sus_platform_cache = {}
    else:
        sus_platform_cache = {}

def save_sus_platform_cache():
    try:
        SUS_PLATFORM_CACHE_PATH.write_text(json.dumps(sus_platform_cache, indent=2))
    except Exception as e:
        print("Failed to save sus_platform_cache:", e)

def set_sus_platform_snapshot(user_id: int, platforms: List[str]):
    try:
        sus_platform_cache[str(user_id)] = {"platforms": platforms, "ts": datetime.datetime.utcnow().timestamp()}
        save_sus_platform_cache()
    except Exception as e:
        print("Error setting sus platform snapshot:", e)

def pop_sus_platform_snapshot(user_id: int):
    try:
        if str(user_id) in sus_platform_cache:
            sus_platform_cache.pop(str(user_id), None)
            save_sus_platform_cache()
    except Exception as e:
        print("Error popping sus platform snapshot:", e)

# -------------------------
# Admin detection helper
# -------------------------
def is_admin_member(member: discord.Member) -> bool:
    if not member:
        return False
    try:
        if member.guild and getattr(member.guild, "owner_id", None) == member.id:
            return True
    except Exception:
        pass
    try:
        if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
            return True
    except Exception:
        pass
    try:
        for r in member.roles:
            if r.id in ADMIN_ROLE_IDS_SET:
                return True
    except Exception:
        pass
    return False

# -------------------------
# Logging helper (non-notifying)
# -------------------------
async def log_to_channel(guild: discord.Guild, text: str, csv_path: str = None):
    channel_id = config.get("log_channel_id") or SUS_LOG_CHANNEL_ID
    if not channel_id:
        print("[LOG]", text)
        return
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            print("[LOG] channel not available, fallback to console:", text)
            return
    try:
        # disable allowed_mentions to avoid accidental pings
        await ch.send(content=text, allowed_mentions=discord.AllowedMentions.none())
        if csv_path:
            await ch.send(file=discord.File(csv_path), allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print("Failed to send log:", e)

# -------------------------
# Role queue to avoid ratelimits
# -------------------------
async def role_worker():
    delay = (config.get("process_delay_ms") or PROCESS_DELAY_MS) / 1000.0
    while True:
        task_coro = await role_queue.get()
        try:
            await task_coro
        except Exception as e:
            print("Role task error:", e)
        await asyncio.sleep(delay)
        role_queue.task_done()

# -------------------------
# Role management helpers
# -------------------------
async def ensure_sus_role_and_overwrites(guild: discord.Guild):
    sus_role_id = config.get("sus_role_id")
    role = None
    if sus_role_id:
        role = guild.get_role(sus_role_id)
    if role is None:
        role = discord.utils.get(guild.roles, name=SUS_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(name=SUS_ROLE_NAME, reason="Create Sus role for verification")
        except Exception as e:
            print("Could not create Sus role:", e)
            return None
    config["sus_role_id"] = role.id
    save_config()

    allowed = {VERIFY_CHANNEL_ID, SUS_CHAT_CHANNEL_ID}
    for ch in guild.channels:
        try:
            if ch.id in allowed:
                await ch.set_permissions(role, view_channel=True, send_messages=True)
            else:
                await ch.set_permissions(role, view_channel=False)
        except Exception:
            pass
    return role

async def send_immediate_mention(guild: discord.Guild, user_id: int):
    """
    Send a plain mention visible to moderators in the verify channel but DO NOT notify the user.
    This uses AllowedMentions.none() so the message includes the mention text but does not produce a ping.
    """
    ttl = config.get("periodic_mention_delete_seconds", 30)
    try:
        ch = guild.get_channel(VERIFY_CHANNEL_ID) or await guild.fetch_channel(VERIFY_CHANNEL_ID)
        sent = await ch.send(
            f"<@{user_id}> (moderation note) You were placed into verification. Please verify.",
            allowed_mentions=discord.AllowedMentions.none()
        )
        await asyncio.sleep(ttl)
        try:
            await sent.delete()
        except Exception:
            pass
    except Exception:
        pass

# -------------------------
# Presence normalization
# -------------------------
def _status_value_to_str(val) -> str:
    try:
        if val is None:
            return "offline"
        s = str(val)
        if not s:
            return "offline"
        return s.lower()
    except Exception:
        return "offline"

def get_member_platforms(member: discord.Member) -> List[str]:
    try:
        platforms: Set[str] = set()
        # direct per-attr status on Member
        for attr, name in (("desktop_status", "desktop"), ("mobile_status", "mobile"), ("web_status", "web")):
            if hasattr(member, attr):
                val = getattr(member, attr)
                if _status_value_to_str(val) != "offline":
                    platforms.add(name)
        cs = getattr(member, "client_status", None) or getattr(member, "_client_status", None)
        if cs:
            if isinstance(cs, dict):
                for k, v in cs.items():
                    if _status_value_to_str(v) != "offline":
                        key = str(k).lower()
                        if "web" in key:
                            platforms.add("web")
                        elif "mobile" in key or "phone" in key or "android" in key or "ios" in key:
                            platforms.add("mobile")
                        elif "desktop" in key or "pc" in key:
                            platforms.add("desktop")
            else:
                for k in ("web", "mobile", "desktop"):
                    if hasattr(cs, k):
                        v = getattr(cs, k)
                        if _status_value_to_str(v) != "offline":
                            platforms.add(k)
        pres = getattr(member, "presence", None) or getattr(member, "_presence", None)
        if pres:
            cs2 = getattr(pres, "client_status", None) or getattr(pres, "_client_status", None)
            if cs2:
                if isinstance(cs2, dict):
                    for k, v in cs2.items():
                        if _status_value_to_str(v) != "offline":
                            key = str(k).lower()
                            if "web" in key:
                                platforms.add("web")
                            elif "mobile" in key or "phone" in key or "android" in key or "ios" in key:
                                platforms.add("mobile")
                            elif "desktop" in key or "pc" in key:
                                platforms.add("desktop")
                else:
                    for k in ("web", "mobile", "desktop"):
                        if hasattr(cs2, k):
                            v = getattr(cs2, k)
                            if _status_value_to_str(v) != "offline":
                                platforms.add(k)
        return sorted(platforms)
    except Exception as e:
        print("get_member_platforms error:", e)
        traceback.print_exc()
        return []

# -------------------------
# Add/remove sus role (queued) — snapshot + immediate no-ping mention
# -------------------------
async def add_sus_role_to_member(member: discord.Member, reason: str = "Marked Sus"):
    role_id = config.get("sus_role_id")
    if not role_id:
        return
    if any(r.id == role_id for r in member.roles):
        await log_to_channel(member.guild, f"User already Sus: {member} (id {member.id})")
        return

    # snapshot platforms before role add
    try:
        snapshot = get_member_platforms(member)
        set_sus_platform_snapshot(member.id, snapshot)
    except Exception as e:
        print("Failed to capture platform snapshot:", e)

    async def op():
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role, reason=reason)

                # short pause for Discord to apply permission overwrites (so moderators' view resolves mention)
                await asyncio.sleep(0.5)

                try:
                    m_refreshed = await member.guild.fetch_member(member.id)
                except Exception:
                    m_refreshed = member

                platforms_now = get_member_platforms(m_refreshed) or snapshot
                await log_to_channel(member.guild, f"User: {member}\nServer Nickname: {member.display_name}\nID: {member.id}\nMention: <@{member.id}>\nPlatform(s): {', '.join(platforms_now)}\nAction: {reason}")

                # Send one immediate moderation-visible mention (no-notify)
                await send_immediate_mention(member.guild, member.id)
            except Exception as e:
                print("Failed to add Sus or send mention:", e)
    await role_queue.put(op())

async def remove_sus_role_from_member(member: discord.Member, by_user: discord.User = None, reason: str = "Verified"):
    role_id = config.get("sus_role_id")
    if not role_id:
        return
    if not any(r.id == role_id for r in member.roles):
        pop_sus_platform_snapshot(member.id)
        return
    async def op():
        role = member.guild.get_role(role_id)
        try:
            await member.remove_roles(role, reason=f"{reason} by {by_user if by_user else 'system'}")
            pop_sus_platform_snapshot(member.id)
            await log_to_channel(member.guild, f"✅\nUser: {member}\nServer Nickname: {member.display_name}\nID: {member.id}\nMention: <@{member.id}>\nPlatform(s): {', '.join(get_member_platforms(member))}\nAction: {reason} by {f'<@{by_user.id}>' if by_user else 'system'}")
        except Exception as e:
            print("Failed to remove Sus:", e)
    await role_queue.put(op())

async def delete_all_bot_messages_in_verify_channel(guild: discord.Guild):
    try:
        ch = guild.get_channel(VERIFY_CHANNEL_ID) or await guild.fetch_channel(VERIFY_CHANNEL_ID)
        if not hasattr(ch, "history"):
            return
        async for m in ch.history(limit=500):
            if m.author and m.author.id == bot.user.id:
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

def build_persistent_verify_text():
    methods = ", ".join(config.get("verification_methods", ["button"]))
    return ("\n".join([
        "**Server verification — click Verify below to begin**",
        "",
        "You were placed into verification. Don’t worry — verifying will restore access if this was a mistake.",
        "",
        "Please click **Verify** in this channel and follow the private instructions to regain access.",
        "",
        f"Methods enabled: {methods}"
    ]))

# -------------------------
# Admin prompt posting (non-notifying)
# -------------------------
async def send_admin_setup_prompt(guild: discord.Guild):
    try:
        if not VERIFY_CHANNEL_ID:
            print("send_admin_setup_prompt: VERIFY_CHANNEL_ID not configured (0).")
            return None
        try:
            verify_ch = guild.get_channel(VERIFY_CHANNEL_ID) or await guild.fetch_channel(VERIFY_CHANNEL_ID)
        except Exception as e:
            print("send_admin_setup_prompt: failed to fetch verify channel:", repr(e))
            traceback.print_exc()
            return None
        if verify_ch is None:
            print(f"send_admin_setup_prompt: verify channel (ID {VERIFY_CHANNEL_ID}) not found in guild {guild.id}.")
            return None
        bot_member = guild.get_member(bot.user.id) or await guild.fetch_member(bot.user.id)
        perms = verify_ch.permissions_for(bot_member)
        if not (perms.view_channel and perms.send_messages):
            print(f"send_admin_setup_prompt: bot lacks required perms in verify channel (view/send). perms: {perms}")
            return None

        role_mentions: List[str] = []
        for rid in ADMIN_ROLE_IDS:
            try:
                role_obj = guild.get_role(rid)
                if role_obj is None:
                    try:
                        role_obj = await guild.fetch_role(rid)
                    except Exception:
                        role_obj = None
                if role_obj:
                    role_mentions.append(role_obj.mention)
            except Exception:
                pass

        mention_text = (f"{' '.join(role_mentions)} " if role_mentions else "")
        configure_button = discord.ui.Button(label="Configure Verification", custom_id="init_setup", style=discord.ButtonStyle.primary)
        view = discord.ui.View()
        view.add_item(configure_button)
        sent = await verify_ch.send(
            content=f"{mention_text}Please configure verification for this server. Click **Configure Verification** or run `/setupverify` in this channel.",
            view=view,
            allowed_mentions=discord.AllowedMentions.none()
        )
        if sent:
            config["admin_prompt_message_id"] = sent.id
            save_config()
            print(f"send_admin_setup_prompt: posted admin prompt message id={sent.id} in channel {verify_ch.id}")
        return sent
    except Exception as e:
        print("send_admin_setup_prompt error:", repr(e))
        traceback.print_exc()
        return None

# -------------------------
# Interaction UIs
# -------------------------
class MarkSusView(discord.ui.View):
    def __init__(self, guild_id: int, target_id: int, ephemeral: bool = False):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.target_id = target_id
        self.ephemeral = ephemeral

    @discord.ui.button(label="Confirm — mark as Sus", style=discord.ButtonStyle.danger, custom_id="mark_sus_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        invoker = interaction.user
        member = interaction.guild.get_member(invoker.id) or await interaction.guild.fetch_member(invoker.id)
        if not is_admin_member(member):
            return await interaction.response.send_message("Only configured admins may confirm marking Sus.", ephemeral=True)
        try:
            target = interaction.guild.get_member(self.target_id) or await interaction.guild.fetch_member(self.target_id)
        except Exception:
            target = None
        if not target:
            return await interaction.response.send_message("Target member not found.", ephemeral=True)
        await add_sus_role_to_member(target, reason=f"Marked Sus via manual scan by {invoker}")
        try:
            await interaction.response.edit_message(content=f"✅ {target.mention} has been marked Sus and logged.", view=None)
        except Exception:
            try:
                await interaction.response.send_message(f"✅ {target.mention} has been marked Sus and logged.", ephemeral=True)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="mark_sus_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="Cancelled — no action taken.", view=None)
        except Exception:
            try:
                await interaction.response.send_message("Cancelled — no action taken.", ephemeral=True)
            except Exception:
                pass
        self.stop()

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="verify_button")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        methods = config.get("verification_methods", ["button"])
        member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
        if not member:
            return await interaction.followup.send("Could not fetch your member record.", ephemeral=True)
        sus_role_id = config.get("sus_role_id")
        if methods == ["button"]:
            if sus_role_id and any(r.id == sus_role_id for r in member.roles):
                await remove_sus_role_from_member(member, by_user=None, reason="Verified via button")
                await interaction.followup.send("You have been verified. ✅", ephemeral=True)
            else:
                await interaction.followup.send("You are not marked for verification.", ephemeral=True)
            return
        enabled = [m for m in methods if m in ("word", "math")]
        if not enabled:
            return await interaction.followup.send("No verification methods are enabled; contact an admin.", ephemeral=True)
        chosen = random.choice(enabled)
        key = f"{interaction.guild_id}-{interaction.user.id}"
        if chosen == "word":
            word = ''.join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6))
            challenge = {"type":"word","answer":word,"expires_at":(datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).timestamp()}
            prompt = f"Type this exact word (private): **{word}**"
        else:
            a = random.randint(2,12); b = random.randint(2,12)
            op = random.choice(["+","*"])
            expr = f"{a} {op} {b}"
            ans = str(a + b if op == "+" else a * b)
            challenge = {"type":"math","answer":ans,"prompt":expr,"expires_at":(datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).timestamp()}
            prompt = f"Solve (private): **{expr}**"
        challenge["platforms"] = get_member_platforms(member)
        challenge_store[key] = challenge
        submit = discord.ui.Button(label="Submit Answer", custom_id=f"open_modal_{interaction.user.id}", style=discord.ButtonStyle.primary)
        view = discord.ui.View()
        view.add_item(submit)
        await interaction.followup.send(f"🔒 **Private challenge** — {prompt}\n\nClick **Submit Answer** to open the secure answer dialog. Your answer will be private and visible only to you.", view=view, ephemeral=True)

class VerifyModal(discord.ui.Modal, title="Enter your answer (private)"):
    answer = discord.ui.TextInput(label="Answer", style=discord.TextStyle.short, placeholder="Type your answer here", max_length=100)
    def __init__(self, guild_id: int, user_id: int, label: str = "Answer"):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
    async def on_submit(self, interaction: discord.Interaction):
        key = f"{self.guild_id}-{self.user_id}"
        ch = challenge_store.get(key)
        if not ch:
            await interaction.response.send_message("No active challenge found or it expired. Click Verify again.", ephemeral=True)
            return
        if datetime.datetime.utcnow().timestamp() > ch.get("expires_at", 0):
            challenge_store.pop(key, None)
            await interaction.response.send_message("Challenge expired. Click Verify again to start a new one.", ephemeral=True)
            return
        submitted = self.answer.value.strip()
        if submitted.lower() == str(ch["answer"]).lower():
            guild = interaction.guild
            member = guild.get_member(self.user_id) or await guild.fetch_member(self.user_id)
            if not member:
                await interaction.response.send_message("Member record not found.", ephemeral=True)
                return
            if config.get("sus_role_id"):
                await remove_sus_role_from_member(member, by_user=interaction.user, reason="Verified via challenge")
            await log_to_channel(guild, f"✅\nUser: {member}\nServer Nickname: {member.display_name}\nID: {member.id}\nMention: <@{member.id}>\nPlatform(s): {', '.join(ch.get('platforms', []))}\nAction: verified via challenge")
            challenge_store.pop(key, None)
            await interaction.response.send_message("✅ Correct — you are verified and can now access the server.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Incorrect answer. Click Verify again to try another challenge.", ephemeral=True)

# -------------------------
# Interactive setup flow
# -------------------------
async def start_interactive_setup(invoker_member: discord.Member, channel: discord.abc.Messageable, sent_message: discord.Message = None):
    try:
        select = discord.ui.Select(
            placeholder="Select verification methods",
            min_values=1,
            max_values=3,
            options=[
                discord.SelectOption(label="Quick Verify Button", value="button", description="One-click verify (fast)"),
                discord.SelectOption(label="Per-user typed word", value="word", description="User types generated word (modal)"),
                discord.SelectOption(label="Math problem", value="math", description="User solves math problem (modal)")
            ]
        )

        confirm_btn = discord.ui.Button(label="Confirm", custom_id="setup_confirm", style=discord.ButtonStyle.success)
        cancel_btn = discord.ui.Button(label="Cancel", custom_id="setup_cancel", style=discord.ButtonStyle.secondary)
        view = discord.ui.View(timeout=120)
        view.add_item(select)
        view.add_item(confirm_btn)
        view.add_item(cancel_btn)

        prompt_text = f"{str(invoker_member)}, choose verification method(s) to enable."
        sent = await channel.send(content=prompt_text, view=view)

        selected = None

        def check(interaction: discord.Interaction):
            return interaction.message and interaction.message.id == sent.id and interaction.user.id == invoker_member.id

        while True:
            try:
                interaction = await bot.wait_for("interaction", timeout=120.0, check=check)
            except asyncio.TimeoutError:
                try:
                    await sent.edit(content=sent.content + "\n\nSetup timed out (no response).", view=None)
                except Exception:
                    pass
                return

            if interaction.type == discord.InteractionType.component:
                data = interaction.data
                cid = data.get("custom_id", "")
                values = data.get("values")
                if isinstance(values, list) and values:
                    selected = values
                    try:
                        await interaction.response.edit_message(content=f"Selected: {', '.join(selected)}. Click Confirm to apply.", view=view)
                    except Exception:
                        try:
                            await interaction.response.send_message(f"Selected: {', '.join(selected)}. Click Confirm to apply.", ephemeral=True)
                        except Exception:
                            pass
                    continue

                if cid == "setup_confirm":
                    await interaction.response.defer(ephemeral=True)
                    if not selected:
                        await interaction.followup.send("Please choose at least one method before confirming.", ephemeral=True)
                        continue
                    config["verification_methods"] = selected
                    await delete_all_bot_messages_in_verify_channel(invoker_member.guild)
                    config["verify_message_id"] = None
                    config["admin_prompt_message_id"] = None
                    await ensure_sus_role_and_overwrites(invoker_member.guild)
                    verify_ch = invoker_member.guild.get_channel(VERIFY_CHANNEL_ID) or await invoker_member.guild.fetch_channel(VERIFY_CHANNEL_ID)
                    if verify_ch and hasattr(verify_ch, "send"):
                        try:
                            m = await verify_ch.send(build_persistent_verify_text(), view=VerifyView())
                            config["verify_message_id"] = m.id
                        except Exception as e:
                            print("Failed to create verify message:", e)
                    save_config()
                    try:
                        await sent.delete()
                    except Exception:
                        pass
                    await interaction.followup.send("Verification configured and previous bot messages removed. New persistent verify message created.", ephemeral=True)
                    return
                elif cid == "setup_cancel":
                    try:
                        await sent.delete()
                    except Exception:
                        pass
                    await interaction.response.send_message("Setup cancelled.", ephemeral=True)
                    return
                else:
                    await interaction.response.send_message("Unhandled component.", ephemeral=True)
                    continue
    except Exception as e:
        print("start_interactive_setup error:", e)
        traceback.print_exc()
        try:
            if sent_message:
                await sent_message.reply("An error occurred during setup. See the logs.", mention_author=False)
        except Exception:
            pass

# -------------------------
# Interaction event handler
# -------------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        if interaction.type == discord.InteractionType.component:
            cid = interaction.data.get("custom_id", "")
            # handle modal opener
            if cid.startswith("open_modal_"):
                try:
                    target_uid = int(cid.split("_")[-1])
                except Exception:
                    return await interaction.response.send_message("Invalid button target.", ephemeral=True)
                if interaction.user.id != target_uid:
                    return await interaction.response.send_message("This button is not for you.", ephemeral=True)
                key = f"{interaction.guild_id}-{interaction.user.id}"
                ch = challenge_store.get(key)
                if not ch:
                    return await interaction.response.send_message("No active challenge found. Click Verify again.", ephemeral=True)
                modal = VerifyModal(interaction.guild_id, interaction.user.id)
                modal.answer.label = (ch.get("prompt") or ("Type this word: "+ch.get("answer")))[:100]
                return await interaction.response.send_modal(modal)

            # handle configure verification button
            if cid == "init_setup":
                member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
                if not is_admin_member(member):
                    return await interaction.response.send_message("You are not allowed to configure verification.", ephemeral=True)
                await interaction.response.defer(ephemeral=True)
                verify_ch = interaction.guild.get_channel(VERIFY_CHANNEL_ID) or await interaction.guild.fetch_channel(VERIFY_CHANNEL_ID)
                await start_interactive_setup(member, verify_ch, sent_message=interaction.message)
                return
    except Exception as e:
        print("on_interaction error:", e)
        traceback.print_exc()

# -------------------------
# New member handling (auto-scan on join)
# -------------------------
@bot.event
async def on_member_join(member: discord.Member):
    """
    Automatically scan a newly joined member and, if they appear to be web-only,
    queue them to receive the Sus role.

    Behavior:
    - Ignore bots
    - Only act for configured GUILD_ID
    - Wait a short time for presence/cache to settle, then fetch a fresh member
    - Use get_member_platforms() and, if platforms == ['web'], queue add_sus_role_to_member()
    """
    try:
        if member.bot:
            return
        if member.guild.id != GUILD_ID:
            return

        print(f"on_member_join: {member} joined guild {member.guild.id}. Starting quick auto-scan...")

        # small delay to give Discord a moment to populate presence/client_status
        await asyncio.sleep(2)

        # ensure sus role exists & channel overwrites are prepared
        try:
            if not config.get("sus_role_id"):
                await ensure_sus_role_and_overwrites(member.guild)
        except Exception as e:
            print("on_member_join: ensure_sus_role_and_overwrites error:", e)

        # fetch a fresh member object
        try:
            fetched = member.guild.get_member(member.id) or await member.guild.fetch_member(member.id)
        except Exception:
            fetched = member

        # check platforms; use cache snapshot fallback if available
        platforms = get_member_platforms(fetched)
        print(f"on_member_join: platforms for {member.id}: {platforms}")

        # If platforms list is exactly ['web'], mark Sus (queue the operation).
        if isinstance(platforms, list) and len(platforms) == 1 and platforms[0] == "web":
            print(f"on_member_join: {member} appears web-only — queuing Sus role")
            # schedule operation and do not block the join event
            asyncio.create_task(add_sus_role_to_member(fetched, reason="Detected web-only on join"))
    except Exception as e:
        print("on_member_join error:", e)
        traceback.print_exc()

# -------------------------
# Scanning & perform_scan (with snapshot fallback)
# -------------------------
async def perform_scan(guild: discord.Guild, member: discord.Member = None, duration: str = None, start_iso: str = None, end_iso: str = None):
    rows = []
    print(f"perform_scan: start (member={'YES' if member else 'BULK'}, duration={duration}, start={start_iso}, end={end_iso})")
    if member:
        try:
            platforms = get_member_platforms(member)
            if not platforms:
                snap = sus_platform_cache.get(str(member.id))
                if snap and (datetime.datetime.utcnow().timestamp() - float(snap.get("ts", 0)) < 86400):
                    platforms = snap.get("platforms", [])
            rows.append({
                "userId": member.id,
                "tag": str(member),
                "displayName": member.display_name,
                "platforms": platforms,
                "joinedAt": member.joined_at.isoformat() if member.joined_at else ""
            })
        except Exception as e:
            print("perform_scan single-member error:", e)
            traceback.print_exc()
        print(f"perform_scan: single-member result rows={len(rows)}")
        return rows

    members_list: List[discord.Member] = []
    try:
        cached_count = len(guild.members)
    except Exception:
        cached_count = 0

    if cached_count and cached_count > 1:
        members_list = list(guild.members)
        print(f"perform_scan: using cached guild.members (count={len(members_list)})")
    else:
        print("perform_scan: guild.members cache empty or small; fetching members via API.")
        try:
            members_list = [m async for m in guild.fetch_members(limit=None)]
            print(f"perform_scan: fetched members count={len(members_list)}")
        except Exception as e:
            print("perform_scan: fetch_members failed:", e)
            traceback.print_exc()
            try:
                members_list = list(guild.members)
                print(f"perform_scan: fallback to cached members count={len(members_list)})")
            except Exception:
                members_list = []

    if not members_list:
        print("perform_scan: no members available to scan.")
        return rows

    now_ts = datetime.datetime.utcnow().timestamp()
    for m in members_list:
        try:
            if m.bot:
                continue
            include = True
            if duration or start_iso or end_iso:
                if not m.joined_at:
                    include = False
                else:
                    jd = m.joined_at.replace(tzinfo=datetime.timezone.utc).timestamp()
                    if duration:
                        mapping = {
                            "last_hour": 3600,
                            "last_day": 86400,
                            "last_week": 86400*7,
                            "last_month": 86400*30
                        }
                        ms = mapping.get(duration)
                        if ms and jd < (now_ts - ms):
                            include = False
                    if start_iso:
                        try:
                            if jd < datetime.datetime.fromisoformat(start_iso).replace(tzinfo=datetime.timezone.utc).timestamp():
                                include = False
                        except Exception:
                            pass
                    if end_iso:
                        try:
                            if jd > datetime.datetime.fromisoformat(end_iso).replace(tzinfo=datetime.timezone.utc).timestamp():
                                include = False
                        except Exception:
                            pass
            if not include:
                continue
            platforms = get_member_platforms(m)
            if not platforms:
                snap = sus_platform_cache.get(str(m.id))
                if snap and (now_ts - float(snap.get("ts", 0)) < 86400):
                    platforms = snap.get("platforms", [])
            rows.append({
                "userId": m.id,
                "tag": str(m),
                "displayName": m.display_name,
                "platforms": platforms,
                "joinedAt": m.joined_at.isoformat() if m.joined_at else ""
            })
        except Exception as exc:
            print("perform_scan: error processing member", getattr(m, "id", "<unknown>"), exc)
            traceback.print_exc()
    print(f"perform_scan: complete, matched rows={len(rows)}")
    return rows

def create_csv_for_scan(rows: List[Dict[str,Any]]) -> str:
    fname = f"scan_{int(datetime.datetime.utcnow().timestamp())}.csv"
    with open(fname, "w", newline='', encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["userId","tag","displayName","platforms","joinedAt"])
        for r in rows:
            writer.writerow([r["userId"], r["tag"], r["displayName"], "|".join(r.get("platforms",[])), r.get("joinedAt","")])
    return fname

async def periodic_notifier():
    if not config.get("periodic_notify_enabled", True):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    role_id = config.get("sus_role_id")
    if not role_id:
        return
    role = guild.get_role(role_id)
    if not role:
        return
    suspects = [m for m in role.members if not m.bot]
    if not suspects:
        return
    ch = guild.get_channel(VERIFY_CHANNEL_ID) or await guild.fetch_channel(VERIFY_CHANNEL_ID)
    ttl = config.get("periodic_mention_delete_seconds", 30)
    chunk_size = 50
    for i in range(0, len(suspects), chunk_size):
        chunk = suspects[i:i+chunk_size]
        # build mention strings but disable allowed_mentions to avoid pings
        mentions = " ".join(f"<@{m.id}>" for m in chunk)
        try:
            sent = await ch.send(f"{mentions} Please complete verification to regain access. Click **Verify** below.", allowed_mentions=discord.AllowedMentions.none())
            await asyncio.sleep(ttl)
            try:
                await sent.delete()
            except Exception:
                pass
        except Exception:
            pass
    await log_to_channel(guild, f"Periodic notifier triggered: mentioned {len(suspects)} Sus members.")

# -------------------------
# Events & startup (load cache)
# -------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Effective intents at runtime:", json.dumps({
        "members": bot.intents.members,
        "presences": bot.intents.presences,
        "message_content": bot.intents.message_content,
        "guilds": bot.intents.guilds
    }, indent=2))
    load_config()
    load_sus_platform_cache()
    global role_worker_task
    if role_worker_task is None:
        role_worker_task = asyncio.create_task(role_worker())
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Bot not in configured guild. Check GUILD_ID.")
        return
    await ensure_sus_role_and_overwrites(guild)

    # debug app commands visible
    try:
        cmds = await bot.tree.fetch_commands(guild=discord.Object(id=GUILD_ID))
        print(f"bot.tree.fetch_commands sees {len(cmds)} guild commands:")
        for c in cmds:
            print("  -", c.name, "| options:", [o.name for o in getattr(c, "options", [])])
    except Exception as e:
        print("bot.tree.fetch_commands() failed:", repr(e))
        traceback.print_exc()

    try:
        raw_cmds = await bot.http.request(discord.http.Route("GET", f"/applications/{bot.user.id}/guilds/{GUILD_ID}/commands"))
        print("Raw REST GET for guild commands (using this bot token) returned:")
        for rc in raw_cmds:
            print("  -", rc.get("name"), "id:", rc.get("id"))
    except Exception as e:
        print("Raw REST GET failed:", repr(e))
        traceback.print_exc()

    print("NOTE: bot will NOT auto-sync application commands at startup (to avoid overwriting).")

    # admin prompt creation
    try:
        posted = False
        try:
            ch = guild.get_channel(VERIFY_CHANNEL_ID) or await guild.fetch_channel(VERIFY_CHANNEL_ID)
        except Exception:
            ch = None
        if ch:
            vmid = config.get("verify_message_id")
            valid = False
            if vmid:
                try:
                    m = await ch.fetch_message(vmid)
                    if m:
                        desired = build_persistent_verify_text()
                        if m.content != desired:
                            try:
                                await m.edit(content=desired)
                            except Exception:
                                pass
                        valid = True
                except Exception:
                    valid = False
            if not valid:
                apid = config.get("admin_prompt_message_id")
                have_prompt = False
                if apid:
                    try:
                        pm = await ch.fetch_message(apid)
                        if pm:
                            have_prompt = True
                    except Exception:
                        have_prompt = False
                if not have_prompt:
                    sent = await send_admin_setup_prompt(guild)
                    if sent:
                        print("Admin setup prompt posted in verify channel.")
                        posted = True
                else:
                    print("Admin setup prompt already exists.")
        else:
            print(f"on_ready: verify channel (ID {VERIFY_CHANNEL_ID}) could not be found or fetched. admin prompt not posted.")
        if not posted:
            print("on_ready: admin prompt not posted (either existing prompt found, or posting failed). Check previous logs for details.")
    except Exception as e:
        print("on_ready admin prompt setup failed:", repr(e))
        traceback.print_exc()

    try:
        spec = config.get("periodic_notify_cron", DEFAULT_CONFIG["periodic_notify_cron"])
        cron_job = aiocron.crontab(spec, func=periodic_notifier, tz=pytz.timezone("Asia/Beirut"), start=False)
        cron_job.start()
        print("Periodic notifier scheduled:", spec)
    except Exception as e:
        print("Failed to schedule periodic notifier:", e)
        traceback.print_exc()

# -------------------------
# Prefix commands handler (complete, robust mention parsing)
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    preview = (message.content[:200] + "...") if message.content and len(message.content) > 200 else (message.content or "<empty>")
    print("INCOMING MESSAGE (bot):", {
        "author": str(message.author),
        "id": message.author.id,
        "channel": message.channel.id,
        "len": len(message.content) if message.content is not None else None,
        "preview": repr(preview)
    })

    if not message.content:
        return

    content_raw = message.content
    content = content_raw.lstrip()
    started_with_prefix = content.startswith(COMMAND_PREFIX)
    print(f"  -> content startswith prefix? {started_with_prefix} (COMMAND_PREFIX={COMMAND_PREFIX!r})")

    if not started_with_prefix:
        return

    body = content[len(COMMAND_PREFIX):].strip()
    args = body.split()
    if not args:
        print("  -> No command after prefix, ignoring.")
        return
    cmd = args[0].lower()
    print(f"  -> Detected prefix command: {cmd} args={args[1:]}")

    member = message.guild.get_member(message.author.id)
    if not member:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except Exception:
            member = None
    is_admin = is_admin_member(member)
    print(f"  -> is_admin: {is_admin}")

    # HELP
    if cmd == "help":
        help_text = (
            "Available prefix commands:\n"
            f"- `{COMMAND_PREFIX}ping` — quick ping test\n"
            f"- `{COMMAND_PREFIX}setlog #channel` — set log channel (admin)\n"
            f"- `{COMMAND_PREFIX}scan [options]` — scan members (admin). Examples:\n"
            "    - `!scan` (bulk scan)\n"
            "    - `!scan last_day` (filter by join time)\n"
            "    - `!scan @user` (single user)\n"
            "    - `!scan last_day apply` (scan + mark web-only as Sus)\n"
            f"- `{COMMAND_PREFIX}setupverify` — open interactive setup (admin, run in verify channel)\n"
            f"- `{COMMAND_PREFIX}verifyuser @user` / `{COMMAND_PREFIX}unsus @user` — manually remove Sus (admin)\n"
            f"- `{COMMAND_PREFIX}autoscan on|off` — toggle autoscan (admin)\n"
        )
        return await message.reply(help_text)

    # PING
    if cmd == "ping":
        try:
            await message.channel.send("pong")
        except Exception as e:
            print("Failed to send pong:", e)
        return

    # SETLOG
    if cmd == "setlog":
        if not is_admin:
            print("  -> setlog denied: not admin")
            return await message.reply("Only configured admin roles may run this command.")
        if len(args) < 2:
            return await message.reply("Usage: !setlog #channel or !setlog CHANNEL_ID")
        mention = args[1]
        m = re.match(r'^<#?(\d{17,20})>?$', mention)
        if not m:
            return await message.reply("Invalid channel mention/ID")
        cid = int(m.group(1))
        ch = message.guild.get_channel(cid) or await message.guild.fetch_channel(cid)
        if not ch or not hasattr(ch, "send"):
            return await message.reply("Channel not found or not text-based.")
        config["log_channel_id"] = cid
        save_config()
        return await message.reply(f"Log channel updated to {ch.mention}")

    # VERIFYUSER / UNSUS
    if cmd in ("unsus", "verifyuser"):
        if not is_admin:
            print("  -> verifyuser denied: not admin")
            return await message.reply("Only configured admin roles may run this command.")
        if not message.mentions:
            return await message.reply("Mention a user: !unsus @user")
        target = message.mentions[0]
        await remove_sus_role_from_member(target, by_user=message.author, reason="Manual unsus via prefix command")
        return await message.reply(f"Removed Sus role (if present) from <@{target.id}>. Logged to <#{config.get('log_channel_id')}>.")

    # AUTOSCAN
    if cmd == "autoscan":
        if not is_admin:
            return await message.reply("Only configured admins can run this.")
        if len(args) < 2:
            return await message.reply("Usage: !autoscan on|off")
        action = args[1].lower()
        config["autoscan_enabled"] = (action == "on")
        save_config()
        return await message.reply(f"Auto-scan is now {'ENABLED' if config['autoscan_enabled'] else 'DISABLED'}.")

    # SETUPVERIFY
    if cmd == "setupverify":
        if not is_admin:
            return await message.reply("You are not allowed to configure verification.")
        verify_ch = message.guild.get_channel(VERIFY_CHANNEL_ID) or await message.guild.fetch_channel(VERIFY_CHANNEL_ID)
        if verify_ch and verify_ch.id != message.channel.id:
            return await message.reply(f"Run this command inside the configured verify channel (ID {VERIFY_CHANNEL_ID}).")
        await message.reply("Opening interactive setup in this channel...")
        await start_interactive_setup(member, verify_ch)
        return

    # SCAN
    if cmd == "scan":
        if not is_admin:
            return await message.reply("Only configured admins can run this.")

        # First preference: if the message includes a mention, use that Member object (reliable)
        member_target = None
        if message.mentions:
            member_target = message.mentions[0]
        else:
            # fall back to parsing args for IDs or keywords
            duration = None
            apply_sus = False
            for a in args[1:]:
                token = a.strip().strip("\\")
                if token.lower() in ("apply", "--apply"):
                    apply_sus = True
                elif re.match(r'^<@!?\d+>$', token) or re.match(r'^\d{17,20}$', token):
                    m = re.search(r'(\d{17,20})', token)
                    if m:
                        uid = int(m.group(1))
                        try:
                            member_target = message.guild.get_member(uid) or await message.guild.fetch_member(uid)
                        except Exception:
                            member_target = None
                elif token.lower() in ("last_hour","last_day","last_week","last_month"):
                    duration = token.lower()

        # If we didn't set duration/apply_sus above because we used mentions, parse args now:
        duration = None
        apply_sus = False
        for a in args[1:]:
            token = a.strip().strip("\\")
            if token.lower() in ("apply", "--apply"):
                apply_sus = True
            elif token.lower() in ("last_hour","last_day","last_week","last_month"):
                duration = token.lower()

        print(f"scan command invoked (member_target={'yes' if member_target else 'no'}, duration={duration}, apply_sus={apply_sus})")

        try:
            async with message.channel.typing():
                rows = await perform_scan(message.guild, member=member_target, duration=duration)
        except Exception as e:
            print("Typing context failed or scan error; running scan without typing:", e)
            traceback.print_exc()
            try:
                rows = await perform_scan(message.guild, member=member_target, duration=duration)
            except Exception as e2:
                print("Prefix scan perform_scan error:", e2)
                traceback.print_exc()
                return await message.reply("Error during scan (see console).")

        if member_target:
            if not rows:
                return await message.reply("Member not found or has no presence info.")
            r = rows[0]
            platforms = r.get("platforms", [])
            platforms_text = ", ".join(platforms) or "offline/no-presence"
            if set(platforms) == {"web"}:
                view = MarkSusView(message.guild.id, member_target.id)
                try:
                    await message.reply(f"User {member_target.mention} appears to be web-only ({platforms_text}). Mark as Sus?", view=view)
                except Exception:
                    await message.reply(f"User {member_target.mention} appears to be web-only. Run `!verifyuser @{member_target.id}` to mark Sus manually.")
                return
            return await message.reply(f"Platforms for {r['tag']}: {platforms_text}\nID: {r['userId']}\nJoined: {r['joinedAt']}")

        if not rows:
            return await message.reply("No members matched the criteria.")
        if len(rows) <= 300:
            header = "user | server nickname | id | mention | platform(s)"
            body = "\n".join([f"{r['tag']} | {r['displayName']} | {r['userId']} | <@{r['userId']}> | {', '.join(r.get('platforms',[]))}" for r in rows])
            try:
                await log_to_channel(message.guild, f"Bulk scan completed ({len(rows)} members):\n{header}\n{body}")
            except Exception as e:
                print("Failed to send scan log:", e)
            await message.reply("Bulk scan complete and logged.")
        else:
            try:
                csv_path = create_csv_for_scan(rows)
                await log_to_channel(message.guild, f"Bulk scan completed: {len(rows)} members — CSV attached.", csv_path)
                await message.reply("Bulk scan complete and CSV uploaded to the log channel.")
                try:
                    os.remove(csv_path)
                except Exception:
                    pass
            except Exception as e:
                print("Failed to create/upload CSV:", e)
                await message.reply("Scan completed but failed to create CSV (see console).")
        if apply_sus:
            suspects = [r for r in rows if len(r.get("platforms",[]))==1 and r["platforms"][0]=="web"]
            if suspects:
                for s in suspects:
                    try:
                        m = message.guild.get_member(int(s["userId"])) or await message.guild.fetch_member(int(s["userId"]))
                        await add_sus_role_to_member(m, reason="Marked via scan applySus")
                    except Exception:
                        pass
                await log_to_channel(message.guild, f"Applied Sus to {len(suspects)} users (queued).")
                await message.reply(f"Applied Sus to {len(suspects)} users (queued).")
        return

    print(f"  -> Unknown prefix command: {cmd} (no action taken)")
    try:
        await bot.process_commands(message)
    except Exception as e:
        print("Error in process_commands:", e)
        traceback.print_exc()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print("Command error:", error)
    traceback.print_exc()

# -------------------------
# Slash commands (full) — /setupverify, /setlog, /verifyuser, /autoscan, /scan
# -------------------------
@bot.tree.command(name="setupverify", description="Interactive setup for verification (run in the verify channel)")
async def setupverify(interaction: discord.Interaction):
    invoker = interaction.user
    member = interaction.guild.get_member(invoker.id) or await interaction.guild.fetch_member(invoker.id)
    if not is_admin_member(member):
        return await interaction.response.send_message("You are not allowed to configure verification.", ephemeral=True)
    verify_ch = interaction.guild.get_channel(VERIFY_CHANNEL_ID) or await interaction.guild.fetch_channel(VERIFY_CHANNEL_ID)
    if verify_ch.id != interaction.channel_id:
        return await interaction.response.send_message(f"Run this command inside the configured verify channel (ID {VERIFY_CHANNEL_ID}).", ephemeral=True)
    await interaction.response.send_message("Opening interactive setup in this channel...", ephemeral=True)
    await start_interactive_setup(member, verify_ch)

@bot.tree.command(name="setlog", description="Set the channel where verification & sus logs should be sent.")
@app_commands.describe(channel="Text channel to use as logs")
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
    if not is_admin_member(member):
        return await interaction.response.send_message("Only configured admins can run this command.", ephemeral=True)
    config["log_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="verifyuser", description="Manually verify (remove Sus role) from a user.")
@app_commands.describe(member="Member to verify")
async def verifyuser(interaction: discord.Interaction, member: discord.Member):
    inv = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
    if not is_admin_member(inv):
        return await interaction.response.send_message("Only configured admins can run this command.", ephemeral=True)
    await remove_sus_role_from_member(member, by_user=interaction.user, reason="Manual verify via command")
    await interaction.response.send_message(f"Removed Sus role (if present) from {member.mention}. Logged to the log channel.", ephemeral=True)

@bot.tree.command(name="autoscan", description="Enable or disable automatic daily scanning.")
@app_commands.describe(action="on or off")
async def autoscan(interaction: discord.Interaction, action: str):
    inv = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
    if not is_admin_member(inv):
        return await interaction.response.send_message("Only configured admins can run this command.", ephemeral=True)
    action = action.lower()
    config["autoscan_enabled"] = (action == "on")
    save_config()
    await interaction.response.send_message(f"Auto-scan is now {'ENABLED' if config['autoscan_enabled'] else 'DISABLED'}.", ephemeral=True)

@bot.tree.command(name="scan", description="Scan members for platform usage.")
@app_commands.describe(member="Check one member only", duration="Quick filter by join time", start="Start ISO timestamp", end="End ISO timestamp", apply_sus="If true, ask to mark matched users Sus")
async def scan_interaction(interaction: discord.Interaction, member: discord.Member = None, duration: str = None, start: str = None, end: str = None, apply_sus: bool = False):
    inv = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)
    if not is_admin_member(inv):
        return await interaction.response.send_message("Only configured admins can run this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # If a member was provided by the slash command, attempt to resolve a fresh member object
    target_member = None
    if member:
        try:
            target_member = interaction.guild.get_member(member.id)
            if not target_member:
                target_member = await interaction.guild.fetch_member(member.id)
        except Exception:
            target_member = member

    rows = await perform_scan(interaction.guild, member=target_member, duration=duration, start_iso=start, end_iso=end)

    if target_member:
        if not rows:
            return await interaction.followup.send("Member not found or has no presence info.", ephemeral=True)
        r = rows[0]
        platforms = r.get("platforms", [])
        platforms_text = ", ".join(platforms) or "offline/no-presence"
        if set(platforms) == {"web"}:
            view = MarkSusView(interaction.guild.id, target_member.id, ephemeral=True)
            try:
                return await interaction.followup.send(f"User {target_member.mention} appears to be web-only. Mark as Sus?", view=view, ephemeral=True)
            except Exception:
                return await interaction.followup.send(f"User {target_member.mention} appears to be web-only. Run `/verifyuser member:{target_member.id}` to mark Sus manually.", ephemeral=True)
        return await interaction.followup.send(f"Platforms for {r['tag']}: {platforms_text}\nID: {r['userId']}\nJoined: {r['joinedAt']}", ephemeral=True)

    if not rows:
        return await interaction.followup.send("No members matched the criteria.", ephemeral=True)
    if len(rows) <= 300:
        header = "user | server nickname | id | mention | platform(s)"
        body = "\n".join([f"{r['tag']} | {r['displayName']} | {r['userId']} | <@{r['userId']}> | {', '.join(r.get('platforms',[]))}" for r in rows])
        await log_to_channel(interaction.guild, f"Bulk scan completed ({len(rows)} members):\n{header}\n{body}")
        return await interaction.followup.send("Bulk scan complete and logged.", ephemeral=True)
    else:
        csv_path = create_csv_for_scan(rows)
        await log_to_channel(interaction.guild, f"Bulk scan completed: {len(rows)} members — CSV attached.", csv_path)
        try:
            os.remove(csv_path)
        except Exception:
            pass
        return await interaction.followup.send("Bulk scan complete and CSV uploaded to the log channel.", ephemeral=True)

# -------------------------
# Start
# -------------------------
def main():
    load_config()
    load_sus_platform_cache()
    bot.run(BOT_TOKEN)

if __name__ == "__main__":
    main()
