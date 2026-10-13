import ast
import asyncio
import copy
import inspect
import io
import json
import math
import operator
import os
import random
import re
import secrets
import string
import time
import traceback
import types
import typing
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True  # for welcome messages (enable "Server Members Intent" in the Developer Portal)
intents.message_content = True  # for modmail relaying (enable "Message Content Intent" in the Developer Portal)

class ModBot(commands.Bot):
    async def setup_hook(self):
        # Re-register the verification button so it keeps working after restarts
        self.add_view(VerifyView())
        self.add_dynamic_items(RoleButton)  # role-menu buttons survive restarts
        self.add_view(TicketPanelView())    # ticket buttons survive restarts
        self.add_view(TicketControlView())
        if not tempban_loop.is_running():
            tempban_loop.start()


bot = ModBot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
START_TIME = time.time()

# ──────────────────────────────────────────────────────────────
# Simple JSON storage (economy, warnings, XP)
# ──────────────────────────────────────────────────────────────
DATA_FILE = Path("data.json")


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            d = {}
    else:
        d = {}
    for key in ("economy", "warnings", "xp", "config", "modmail", "tempbans", "tickets", "sticky", "shop", "inventory"):
        d.setdefault(key, {})
    return d


data = load_data()


def save_data() -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


def make_embed(title: str, description: str | None = None, color: discord.Color | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color or discord.Color.blurple())


def hierarchy_problem(guild: discord.Guild, actor: discord.Member, target: discord.Member) -> str | None:
    if target == guild.owner:
        return "You can't do that to the server owner."
    if target == actor:
        return "You can't do that to yourself."
    if target.id == bot.user.id:
        return "Nice try."
    if actor != guild.owner and actor.top_role <= target.top_role:
        return "That member's highest role is equal to or above yours."
    if guild.me.top_role <= target.top_role:
        return "That member's highest role is above mine, so I can't act on them."
    return None


def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> str | None:
    return hierarchy_problem(interaction.guild, interaction.user, target)


# ──────────────────────────────────────────────────────────────
# Events
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    await bot.change_presence(activity=discord.Game(f"/help or {PREFIX}help"))
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) — {len(bot.tree.get_commands())} commands synced")


@bot.event
async def on_member_join(member: discord.Member):
    if await antiraid_check(member):
        return
    cfg = data["config"].get(str(member.guild.id), {})
    autorole = member.guild.get_role(cfg.get("autorole", 0))
    if autorole:
        try:
            await member.add_roles(autorole, reason="Autorole")
        except discord.HTTPException:
            pass
    await log_action(
        member.guild, "📥 Member joined", f"{member.mention} (`{member.id}`)", discord.Color.green(),
        [("Account created", discord.utils.format_dt(member.created_at, "R"))],
    )
    unverified = member.guild.get_role(cfg.get("unverified_role", 0))
    if unverified:
        try:
            await member.add_roles(unverified, reason="Awaiting verification")
        except discord.Forbidden:
            pass
    await restore_sticky_roles(member)
    channel = member.guild.system_channel
    if channel:
        await channel.send(f"Welcome to the server, {member.mention}! 🎉")


def level_for(xp: int) -> int:
    return int(math.sqrt(xp / 50))


xp_cooldowns: dict[tuple[int, int], float] = {}


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        await handle_dm(message)
        return
    if await check_honeypot(message):
        return
    thread = data["modmail"].get(str(message.channel.id))
    if thread:
        if await run_prefix_command(message):  # e.g. -close, -anonymous inside a modmail thread
            return
        await relay_staff_message(message, thread)
        return
    if await run_automod(message):
        return
    if await run_prefix_command(message):
        return
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - xp_cooldowns.get(key, 0) < 60:
        return
    xp_cooldowns[key] = now
    guild_xp = data["xp"].setdefault(str(message.guild.id), {})
    old = guild_xp.get(str(message.author.id), 0)
    new = old + random.randint(15, 25)
    guild_xp[str(message.author.id)] = new
    save_data()
    if level_for(new) > level_for(old):
        await message.channel.send(f"🎉 {message.author.mention} reached **level {level_for(new)}**!")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        needed = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        msg = f"You need the **{needed}** permission to use that command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "That command only works in a server."
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        msg = "I don't have the permissions I need to do that."
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════
# 🎉 FUN
# ══════════════════════════════════════════════════════════════
JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break. Now it won't stop sending me Kit-Kat ads.",
    "Why did the developer go broke? Because he used up all his cache.",
    "There are 10 kinds of people: those who understand binary and those who don't.",
    "A SQL query walks into a bar, sees two tables and asks: 'Can I join you?'",
    "Why was the JavaScript developer sad? He didn't know how to null his feelings.",
    "I'd tell you a UDP joke, but you might not get it.",
    "How many programmers does it take to change a light bulb? None, that's a hardware problem.",
]
FACTS = [
    "Honey never spoils — edible honey has been found in ancient Egyptian tombs.",
    "Octopuses have three hearts and blue blood.",
    "A day on Venus is longer than a year on Venus.",
    "Bananas are berries, but strawberries aren't.",
    "The Eiffel Tower can be about 15 cm taller in summer due to thermal expansion.",
    "Wombat droppings are cube-shaped.",
    "There are more possible chess games than atoms in the observable universe.",
    "Sharks are older than trees.",
]


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! {round(bot.latency * 1000)}ms")


@bot.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides (default 6)", count="How many dice (default 1)")
async def roll(
    interaction: discord.Interaction,
    sides: app_commands.Range[int, 2, 1000] = 6,
    count: app_commands.Range[int, 1, 20] = 1,
):
    rolls = [random.randint(1, sides) for _ in range(count)]
    detail = f" ({', '.join(map(str, rolls))})" if count > 1 else ""
    await interaction.response.send_message(f"🎲 {count}d{sides} → **{sum(rolls)}**{detail}")


@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="Your yes/no question")
async def eight_ball(interaction: discord.Interaction, question: str):
    answers = [
        "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.",
        "Ask again later.", "Cannot predict now.", "Don't count on it.",
        "My sources say no.", "Very doubtful.",
    ]
    await interaction.response.send_message(f"🎱 **{question}**\n{random.choice(answers)}")


@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Heads', 'Tails'])}!")


@bot.tree.command(name="choose", description="Let the bot pick between options")
@app_commands.describe(options="Options separated by commas, e.g. pizza, tacos, sushi")
async def choose(interaction: discord.Interaction, options: str):
    items = [o.strip() for o in options.split(",") if o.strip()]
    if len(items) < 2:
        return await interaction.response.send_message("Give me at least two options, separated by commas.", ephemeral=True)
    await interaction.response.send_message(f"🤔 I choose: **{random.choice(items)}**")


@bot.tree.command(name="rps", description="Play rock paper scissors against the bot")
@app_commands.describe(choice="Your move")
@app_commands.choices(choice=[
    app_commands.Choice(name="Rock 🪨", value="rock"),
    app_commands.Choice(name="Paper 📄", value="paper"),
    app_commands.Choice(name="Scissors ✂️", value="scissors"),
])
async def rps(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    bot_choice = random.choice(["rock", "paper", "scissors"])
    wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    if choice.value == bot_choice:
        result = "It's a tie!"
    elif (choice.value, bot_choice) in wins:
        result = "You win! 🎉"
    else:
        result = "I win! 😎"
    await interaction.response.send_message(f"You chose **{choice.value}**, I chose **{bot_choice}**. {result}")


@bot.tree.command(name="joke", description="Hear a (bad) programming joke")
async def joke(interaction: discord.Interaction):
    await interaction.response.send_message(f"😄 {random.choice(JOKES)}")


@bot.tree.command(name="fact", description="Get a random fun fact")
async def fact(interaction: discord.Interaction):
    await interaction.response.send_message(f"🧠 {random.choice(FACTS)}")


@bot.tree.command(name="say", description="Make the bot say something")
@app_commands.describe(text="What the bot should say")
async def say(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="reverse", description="Reverse some text")
@app_commands.describe(text="Text to reverse")
async def reverse(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(text[::-1], allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="mock", description="cOnVeRt TeXt tO mOcKiNg CaSe")
@app_commands.describe(text="Text to mock")
async def mock(interaction: discord.Interaction, text: str):
    out = "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(text))
    await interaction.response.send_message(out, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="rate", description="Rate anything out of 10")
@app_commands.describe(thing="What should I rate?")
async def rate(interaction: discord.Interaction, thing: str):
    score = zlib.crc32(thing.lower().encode()) % 11
    await interaction.response.send_message(
        f"I rate **{thing}** a **{score}/10** ⭐", allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(name="ship", description="Check the compatibility of two members")
@app_commands.describe(first="First person", second="Second person")
async def ship(interaction: discord.Interaction, first: discord.Member, second: discord.Member):
    pct = random.Random(first.id + second.id).randint(0, 100)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await interaction.response.send_message(f"💘 {first.display_name} × {second.display_name}\n`{bar}` **{pct}%**")


@bot.tree.command(name="hug", description="Give someone a hug")
@app_commands.describe(member="Who to hug")
async def hug(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_message(f"🤗 {interaction.user.mention} gives {member.mention} a big hug!")


@bot.tree.command(name="slap", description="Playfully slap someone")
@app_commands.describe(member="Who to slap")
async def slap(interaction: discord.Interaction, member: discord.Member):
    item = random.choice(["a large trout", "a pillow", "a rubber duck", "a wet noodle"])
    await interaction.response.send_message(f"👋 {interaction.user.mention} slaps {member.mention} with {item}!")


TRIVIA = [
    ("What is the capital of Australia?", ["Sydney", "Canberra", "Melbourne", "Perth"], 1),
    ("Which planet is known as the Red Planet?", ["Venus", "Jupiter", "Mars", "Mercury"], 2),
    ("How many bits are in a byte?", ["4", "8", "16", "32"], 1),
    ("Who painted the Mona Lisa?", ["Michelangelo", "Van Gogh", "Da Vinci", "Picasso"], 2),
    ("What is the largest ocean on Earth?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("Which language is primarily used to style web pages?", ["HTML", "CSS", "Python", "SQL"], 1),
    ("What is the chemical symbol for gold?", ["Au", "Ag", "Gd", "Go"], 0),
]


class TriviaView(discord.ui.View):
    def __init__(self, user: discord.abc.User, options: list[str], correct: int):
        super().__init__(timeout=20)
        self.user = user
        self.correct = correct
        self.message: discord.Message | None = None
        for i, opt in enumerate(options):
            button = discord.ui.Button(label=opt, style=discord.ButtonStyle.primary)
            button.callback = self.make_callback(i)
            self.add_item(button)

    def make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                return await interaction.response.send_message("This isn't your question!", ephemeral=True)
            for child in self.children:
                child.disabled = True
            self.children[self.correct].style = discord.ButtonStyle.success
            if index == self.correct:
                acct = data["economy"].setdefault(str(self.user.id), {"bal": 0, "daily": 0, "work": 0})
                acct["bal"] += 50
                save_data()
                text = "✅ Correct! You earned **50 coins**."
            else:
                self.children[index].style = discord.ButtonStyle.danger
                text = "❌ Wrong answer!"
            self.stop()
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n{text}", view=self)
        return callback

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content=self.message.content + "\n\n⏰ Time's up!", view=self)


@bot.tree.command(name="trivia", description="Answer a trivia question to win 50 coins")
async def trivia(interaction: discord.Interaction):
    question, options, correct = random.choice(TRIVIA)
    view = TriviaView(interaction.user, options, correct)
    await interaction.response.send_message(f"❓ **{question}**", view=view)
    view.message = await interaction.original_response()


# ══════════════════════════════════════════════════════════════
# 🛠️ UTILITY
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = make_embed("📖 Command list")
    embed.add_field(name="🎉 Fun", value="`ping` `roll` `8ball` `coinflip` `choose` `rps` `joke` `fact` `say` `reverse` `mock` `rate` `ship` `hug` `slap` `trivia`", inline=False)
    embed.add_field(name="🛠️ Utility", value="`userinfo` `serverinfo` `avatar` `roleinfo` `membercount` `botinfo` `uptime` `poll` `remind` `calc` `password` `help`", inline=False)
    embed.add_field(name="🛡️ Moderation", value="`kick` `ban` `unban` `timeout` `untimeout` `purge` `slowmode` `lock` `unlock` `warn` `warnings` `clearwarnings` `nick` `addrole` `removerole`", inline=False)
    embed.add_field(name="💰 Economy", value="`balance` `daily` `work` `give` `coinsgift` `gamble` `leaderboard`  ·  admins: `admincoins` (add · remove · set)", inline=False)
    embed.add_field(name="🛍️ Shop", value="`shop` `buy` `inventory` `redeem`  ·  admins: `shopadmin` (add · remove · edit · give)", inline=False)
    embed.add_field(name="📈 Levels", value="`rank` `leaderboard` (XP & coins tabs) — earn XP by chatting", inline=False)
    embed.add_field(name="🤖 Auto-moderation", value="`automod-setup` `automod` (status · toggle · rule · spam · mentions · caps · badword · linkdomain · exempt · strikes) `escalation` `modlog` `antiraid` `raidmode`", inline=False)
    embed.add_field(name="🔧 More moderation", value="`tempban` `softban` `purge-user` `unwarn` `snipe`  ·  right-click a message → Apps → **Report message**", inline=False)
    embed.add_field(name="🎭 Roles", value="`autorole` `rolemenu` `stickyroles` (toggle · add · remove · list · forget)", inline=False)
    embed.add_field(name="🎫 Tickets", value="`ticket-setup` (admin) · inside a ticket: `ticket` (add · remove · rename · notes · claim · close)", inline=False)
    embed.add_field(name="🍯 Honeypot", value="`honeypot` (setup · add · remove · action · exempt · status)", inline=False)
    embed.add_field(name="🛠️ Admin panel", value="`adminpanel` — private control panel for everything · `admin @member` — pick a moderation action, then fill in a form (both slash only)", inline=False)
    embed.add_field(name="✅ Verification", value="`verification-setup`", inline=False)
    embed.add_field(name="📬 Modmail", value="DM the bot to contact staff.\nStaff: `modmail-setup` `modmail-block` `modmail-unblock` `contact` `anonymous` `close`", inline=False)
    embed.set_footer(text=f"Every command also works with the {PREFIX} prefix — e.g. {PREFIX}ping, {PREFIX}kick @user reason")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="Show info about a member")
@app_commands.describe(member="The member to look up (defaults to you)")
@app_commands.guild_only()
async def userinfo(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    embed = make_embed(str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R"))
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
    embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:15]) or "None", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Show info about this server")
@app_commands.guild_only()
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = make_embed(g.name)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown")
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    embed.add_field(name="Text channels", value=len(g.text_channels))
    embed.add_field(name="Voice channels", value=len(g.voice_channels))
    embed.add_field(name="Roles", value=len(g.roles))
    embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Show someone's avatar")
@app_commands.describe(member="Whose avatar (defaults to you)")
async def avatar(interaction: discord.Interaction, member: discord.User | None = None):
    member = member or interaction.user
    embed = make_embed(f"{member.display_name}'s avatar")
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="roleinfo", description="Show info about a role")
@app_commands.describe(role="The role to look up")
@app_commands.guild_only()
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    embed = make_embed(role.name, color=role.color)
    embed.add_field(name="ID", value=role.id)
    embed.add_field(name="Members", value=len(role.members))
    embed.add_field(name="Color", value=str(role.color))
    embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No")
    embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="membercount", description="Show how many members the server has")
@app_commands.guild_only()
async def membercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"👥 **{interaction.guild.member_count}** members")


@bot.tree.command(name="botinfo", description="Show info about the bot")
async def botinfo(interaction: discord.Interaction):
    embed = make_embed(str(bot.user))
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="Servers", value=len(bot.guilds))
    embed.add_field(name="Commands", value=len(bot.tree.get_commands()))
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms")
    embed.add_field(name="discord.py", value=discord.__version__)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="uptime", description="How long the bot has been running")
async def uptime(interaction: discord.Interaction):
    delta = timedelta(seconds=int(time.time() - START_TIME))
    await interaction.response.send_message(f"⏱️ Up for **{delta}**")


@bot.tree.command(name="poll", description="Create a quick reaction poll")
@app_commands.describe(question="The poll question", options="2–9 options separated by commas (leave empty for yes/no)")
@app_commands.guild_only()
async def poll(interaction: discord.Interaction, question: str, options: str | None = None):
    if options:
        items = [o.strip() for o in options.split(",") if o.strip()]
        if not 2 <= len(items) <= 9:
            return await interaction.response.send_message("Give me between 2 and 9 options.", ephemeral=True)
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"][: len(items)]
        body = "\n".join(f"{e} {o}" for e, o in zip(emojis, items))
    else:
        emojis = ["👍", "👎"]
        body = "React with 👍 or 👎"
    embed = make_embed(f"📊 {question}", body)
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for e in emojis:
        await msg.add_reaction(e)


@bot.tree.command(name="remind", description="Set a reminder (lost if the bot restarts)")
@app_commands.describe(minutes="Minutes from now", message="What to remind you about")
async def remind(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440], message: str):
    await interaction.response.send_message(f"⏰ Okay, I'll remind you in **{minutes}** minute(s).")
    await asyncio.sleep(minutes * 60)
    await interaction.channel.send(
        f"{interaction.user.mention} ⏰ Reminder: {message}",
        allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
    )


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(node):
    if isinstance(node, ast.Expression):
        return safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = safe_eval(node.left), safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("Exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](safe_eval(node.operand))
    raise ValueError("Unsupported expression")


@bot.tree.command(name="calc", description="Evaluate a math expression")
@app_commands.describe(expression="e.g. (2 + 3) * 4 ** 2")
async def calc(interaction: discord.Interaction, expression: str):
    try:
        result = safe_eval(ast.parse(expression, mode="eval"))
    except ZeroDivisionError:
        return await interaction.response.send_message("You can't divide by zero.", ephemeral=True)
    except Exception:
        return await interaction.response.send_message("I couldn't evaluate that expression.", ephemeral=True)
    await interaction.response.send_message(f"🧮 `{expression}` = **{result}**")


@bot.tree.command(name="password", description="Generate a random password (only you can see it)")
@app_commands.describe(length="Password length (default 16)")
async def password(interaction: discord.Interaction, length: app_commands.Range[int, 8, 64] = 16):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    pw = "".join(secrets.choice(alphabet) for _ in range(length))
    await interaction.response.send_message(f"🔐 `{pw}`", ephemeral=True)


# ══════════════════════════════════════════════════════════════
# 🛡️ MODERATION
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"👢 Kicked **{member}** — {reason}")


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason", delete_days="Days of messages to delete (0–7)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason given",
    delete_days: app_commands.Range[int, 0, 7] = 0,
):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
    data["tempbans"].pop(f"{interaction.guild.id}:{member.id}", None)
    await interaction.response.send_message(f"🔨 Banned **{member}** — {reason}")


@bot.tree.command(name="unban", description="Unban a user by ID")
@app_commands.describe(user_id="The user's ID", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason given"):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=f"{interaction.user}: {reason}")
        data["tempbans"].pop(f"{interaction.guild.id}:{user.id}", None)
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message("I couldn't find a banned user with that ID.", ephemeral=True)
    await interaction.response.send_message(f"✅ Unbanned **{user}**")


@bot.tree.command(name="timeout", description="Timeout (mute) a member")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason given",
):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"🔇 Timed out **{member}** for {minutes} minute(s) — {reason}")


@bot.tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.describe(member="Member to un-timeout")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f"🔊 Removed timeout from **{member}**")


@bot.tree.command(name="purge", description="Delete recent messages in this channel")
@app_commands.describe(amount="How many messages to delete (1–100)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.purge(limit=amount)
    try:  # no confirmation message: just clear the "thinking…" placeholder
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


@bot.tree.command(name="slowmode", description="Set slowmode for this channel")
@app_commands.describe(seconds="Seconds between messages (0 to disable)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds)
    msg = "Slowmode disabled." if seconds == 0 else f"Slowmode set to {seconds}s."
    await interaction.response.send_message(f"🐌 {msg}")


@bot.tree.command(name="lock", description="Stop @everyone from sending messages in this channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def lock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message("🔒 Channel locked.")


@bot.tree.command(name="unlock", description="Let @everyone send messages in this channel again")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def unlock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = None
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message("🔓 Channel unlocked.")


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.describe(member="Member to warn", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction.response.defer()
    count = add_warning(interaction.guild, member, reason, interaction.user.id)
    try:
        await member.send(f"You were warned in **{interaction.guild.name}**: {reason}")
    except discord.HTTPException:
        pass
    action = await apply_escalation(interaction.guild, member, count)
    await log_action(
        interaction.guild, "⚠️ Warning issued", f"{member.mention} (`{member.id}`)\n**By:** {interaction.user.mention}",
        discord.Color.gold(), [("Reason", reason), ("Total warnings", count)],
    )
    msg = f"⚠️ Warned {member.mention} — {reason} (total: **{count}**)"
    if action:
        msg += f"\n{action}"
    await interaction.followup.send(msg)


@bot.tree.command(name="warnings", description="View a member's warnings")
@app_commands.describe(member="Member to check")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    warns = data["warnings"].get(str(interaction.guild.id), {}).get(str(member.id), [])
    if not warns:
        return await interaction.response.send_message(f"{member} has no warnings.", ephemeral=True)
    lines = [f"**{i}.** {w['reason']} — <@{w['mod']}>" for i, w in enumerate(warns[-10:], max(len(warns) - 9, 1))]
    await interaction.response.send_message(embed=make_embed(f"Warnings for {member} ({len(warns)})", "\n".join(lines)))


@bot.tree.command(name="clearwarnings", description="Clear all of a member's warnings")
@app_commands.describe(member="Member to clear")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    data["warnings"].get(str(interaction.guild.id), {}).pop(str(member.id), None)
    save_data()
    await interaction.response.send_message(f"🧽 Cleared warnings for **{member}**.")


@bot.tree.command(name="nick", description="Change a member's nickname")
@app_commands.describe(member="Member", nickname="New nickname (leave empty to reset)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def nick(interaction: discord.Interaction, member: discord.Member, nickname: str | None = None):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.edit(nick=nickname)
    await interaction.response.send_message(f"✏️ Nickname for **{member.name}** {'set to ' + nickname if nickname else 'reset'}.")


def role_error(interaction: discord.Interaction, role: discord.Role) -> str | None:
    if interaction.user != interaction.guild.owner and role >= interaction.user.top_role:
        return "That role is equal to or above your highest role."
    if role >= interaction.guild.me.top_role:
        return "That role is above my highest role, so I can't manage it."
    return None


@bot.tree.command(name="addrole", description="Give a role to a member")
@app_commands.describe(member="Member", role="Role to give")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def addrole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if err := role_error(interaction, role):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.add_roles(role)
    await interaction.response.send_message(f"✅ Gave **{role.name}** to {member.mention}",
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="removerole", description="Remove a role from a member")
@app_commands.describe(member="Member", role="Role to remove")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if err := role_error(interaction, role):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.remove_roles(role)
    await interaction.response.send_message(f"✅ Removed **{role.name}** from {member.mention}",
                                            allowed_mentions=discord.AllowedMentions.none())


# ══════════════════════════════════════════════════════════════
# 💰 ECONOMY
# ══════════════════════════════════════════════════════════════
def account(user_id: int) -> dict:
    return data["economy"].setdefault(str(user_id), {"bal": 0, "daily": 0, "work": 0})


def fmt_wait(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


@bot.tree.command(name="balance", description="Check your (or someone's) coin balance")
@app_commands.describe(member="Whose balance (defaults to you)")
async def balance(interaction: discord.Interaction, member: discord.User | None = None):
    member = member or interaction.user
    await interaction.response.send_message(f"💰 **{member.display_name}** has **{account(member.id)['bal']:,}** coins")


@bot.tree.command(name="daily", description="Claim your daily coins")
async def daily(interaction: discord.Interaction):
    acct = account(interaction.user.id)
    wait = 86400 - (time.time() - acct["daily"])
    if wait > 0:
        return await interaction.response.send_message(f"⏳ Come back in **{fmt_wait(wait)}**.", ephemeral=True)
    acct["bal"] += 500
    acct["daily"] = time.time()
    save_data()
    await interaction.response.send_message("🎁 You claimed your daily **500 coins**!")


@bot.tree.command(name="work", description="Work for some coins (1 hour cooldown)")
async def work(interaction: discord.Interaction):
    acct = account(interaction.user.id)
    wait = 3600 - (time.time() - acct["work"])
    if wait > 0:
        return await interaction.response.send_message(f"⏳ You're tired. Rest for **{fmt_wait(wait)}**.", ephemeral=True)
    earned = random.randint(50, 200)
    job = random.choice(["fixed some bugs", "delivered pizzas", "walked dogs", "streamed for 3 viewers", "mowed a lawn"])
    acct["bal"] += earned
    acct["work"] = time.time()
    save_data()
    await interaction.response.send_message(f"💼 You {job} and earned **{earned}** coins!")


@bot.tree.command(name="give", description="Give coins to another user")
@app_commands.describe(member="Who to pay", amount="How many coins")
async def give(interaction: discord.Interaction, member: discord.User, amount: app_commands.Range[int, 1, 1_000_000]):
    if member.bot or member.id == interaction.user.id:
        return await interaction.response.send_message("You can't give coins to that user.", ephemeral=True)
    sender = account(interaction.user.id)
    if sender["bal"] < amount:
        return await interaction.response.send_message("You don't have enough coins.", ephemeral=True)
    sender["bal"] -= amount
    account(member.id)["bal"] += amount
    save_data()
    await interaction.response.send_message(f"💸 {interaction.user.mention} gave {member.mention} **{amount:,}** coins!")


@bot.tree.command(name="gamble", description="Bet coins — 45% chance to double them")
@app_commands.describe(amount="How many coins to bet")
async def gamble(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000]):
    acct = account(interaction.user.id)
    if acct["bal"] < amount:
        return await interaction.response.send_message("You don't have enough coins.", ephemeral=True)
    if random.random() < 0.45:
        acct["bal"] += amount
        msg = f"🎰 You won **{amount:,}** coins! Balance: **{acct['bal']:,}**"
    else:
        acct["bal"] -= amount
        msg = f"🎰 You lost **{amount:,}** coins. Balance: **{acct['bal']:,}**"
    save_data()
    await interaction.response.send_message(msg)


# ══════════════════════════════════════════════════════════════
# 📈 LEVELS
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="rank", description="Show your level and XP")
@app_commands.describe(member="Whose rank (defaults to you)")
@app_commands.guild_only()
async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    guild_xp = data["xp"].get(str(interaction.guild.id), {})
    xp = guild_xp.get(str(member.id), 0)
    lvl = level_for(xp)
    next_xp = 50 * (lvl + 1) ** 2
    position = sorted(guild_xp.values(), reverse=True).index(xp) + 1 if xp else len(guild_xp) + 1
    embed = make_embed(f"📈 {member.display_name}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=lvl)
    embed.add_field(name="XP", value=f"{xp:,} / {next_xp:,}")
    embed.add_field(name="Rank", value=f"#{position}")
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════
# ✅ VERIFICATION
# ══════════════════════════════════════════════════════════════
def get_config(guild_id: int) -> dict:
    return data["config"].setdefault(str(guild_id), {})


class VerifyModal(discord.ui.Modal):
    def __init__(self, a: int, b: int):
        super().__init__(title="Verification")
        self.answer = a + b
        self.response_input = discord.ui.TextInput(label=f"What is {a} + {b}?", min_length=1, max_length=3)
        self.add_item(self.response_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.response_input.value.strip() != str(self.answer):
            return await interaction.response.send_message(
                "❌ Wrong answer — click the button to try again.", ephemeral=True
            )
        guild, member = interaction.guild, interaction.user
        cfg = get_config(guild.id)
        role = guild.get_role(cfg.get("verify_role", 0))
        unverified = guild.get_role(cfg.get("unverified_role", 0))
        if role is None:
            return await interaction.response.send_message(
                "Verification isn't set up properly. Please tell a moderator.", ephemeral=True
            )
        try:
            await member.add_roles(role, reason="Passed verification")
            if unverified and unverified in member.roles:
                await member.remove_roles(unverified, reason="Passed verification")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "I couldn't give you the role. Please ask a moderator to move my role above it.", ephemeral=True
            )
        await interaction.response.send_message("✅ You're verified — welcome!", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="verify:button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("verify_role", 0))
        if role is None:
            return await interaction.response.send_message(
                "Verification isn't set up properly. Please tell a moderator.", ephemeral=True
            )
        if role in interaction.user.roles:
            return await interaction.response.send_message("You're already verified!", ephemeral=True)
        await interaction.response.send_modal(VerifyModal(random.randint(1, 12), random.randint(1, 12)))


@bot.tree.command(name="verification-setup", description="Post a verification panel and set the verified role")
@app_commands.describe(
    channel="Channel to post the verification panel in",
    role="Role given to members who verify",
    unverified_role="Optional role given on join and removed on verification",
    message="Text shown on the panel",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def verification_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
    unverified_role: discord.Role | None = None,
    message: str = "Click the button below and answer the question to gain access to the server.",
):
    for r in (role, unverified_role):
        if r is not None and (err := role_error(interaction, r)):
            return await interaction.response.send_message(f"**{r.name}**: {err}", ephemeral=True)
    cfg = get_config(interaction.guild.id)
    cfg["verify_role"] = role.id
    cfg["unverified_role"] = unverified_role.id if unverified_role else 0
    save_data()
    embed = make_embed("✅ Verification", message, discord.Color.green())
    await channel.send(embed=embed, view=VerifyView())
    await interaction.response.send_message(
        f"Verification panel posted in {channel.mention}.\n"
        "Tip: hide your other channels from @everyone (or from the unverified role) so only verified members can see them.",
        ephemeral=True,
    )


# ══════════════════════════════════════════════════════════════
# 📬 MODMAIL
# Users DM the bot → a private channel is created in the modmail category.
# Staff just type in the channel (// = internal note), toggle /anonymous, and finish with /close.
# ══════════════════════════════════════════════════════════════
def thread_for(interaction: discord.Interaction) -> dict | None:
    return data["modmail"].get(str(interaction.channel_id))


def find_thread(guild_id: int, user_id: int) -> discord.abc.GuildChannel | None:
    for cid, t in list(data["modmail"].items()):
        if t["guild"] == guild_id and t["user"] == user_id:
            channel = bot.get_channel(int(cid))
            if channel is None:  # channel was deleted manually
                data["modmail"].pop(cid, None)
                save_data()
                continue
            return channel
    return None


def modmail_guilds(user: discord.abc.User) -> list[discord.Guild]:
    result = []
    for g in bot.guilds:
        cfg = data["config"].get(str(g.id), {})
        category = g.get_channel(cfg.get("modmail_category", 0))
        if (
            g.get_member(user.id)
            and isinstance(category, discord.CategoryChannel)
            and user.id not in cfg.get("blocked", [])
        ):
            result.append(g)
    return result


async def forward_to_thread(channel: discord.TextChannel, message: discord.Message) -> None:
    embed = discord.Embed(
        description=message.content or None, color=discord.Color.blurple(), timestamp=message.created_at
    )
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    if message.attachments:
        embed.add_field(
            name="Attachments", value="\n".join(a.url for a in message.attachments)[:1024], inline=False
        )
        image = next((a for a in message.attachments if (a.content_type or "").startswith("image/")), None)
        if image:
            embed.set_image(url=image.url)
    await channel.send(embed=embed)


async def open_thread(
    guild: discord.Guild,
    user: discord.abc.User,
    first_message: discord.Message | None = None,
    opened_by: discord.Member | None = None,
) -> discord.TextChannel:
    cfg = get_config(guild.id)
    category = guild.get_channel(cfg.get("modmail_category", 0))
    channel = await guild.create_text_channel(
        f"mail-{user.name}",
        category=category,
        topic=f"Modmail thread with {user} ({user.id})",
        reason=f"Modmail thread for {user}",
    )
    data["modmail"][str(channel.id)] = {"user": user.id, "guild": guild.id}
    save_data()

    member = guild.get_member(user.id)
    opener = f"Opened by staff member {opened_by.mention}" if opened_by else f"Opened by {user.mention}"
    info = make_embed("📬 New modmail thread", f"{opener} (`{user.id}`)")
    info.set_thumbnail(url=user.display_avatar.url)
    info.add_field(name="Account created", value=discord.utils.format_dt(user.created_at, "R"))
    if member and member.joined_at:
        info.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R"))
        roles = [r.mention for r in reversed(member.roles) if r != guild.default_role]
        info.add_field(name="Roles", value=" ".join(roles[:10]) or "None", inline=False)
    info.add_field(
        name="How to respond",
        value=(
            "Just type here — your messages are sent to the user (✅ = delivered).\n"
            "`//` = internal note · `/anonymous` toggles anonymous mode · `/close` to finish."
        ),
        inline=False,
    )
    ping = guild.get_role(cfg.get("modmail_role", 0))
    await channel.send(
        content=ping.mention if ping else None,
        embed=info,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )
    if first_message:
        await forward_to_thread(channel, first_message)
        try:
            await user.send(
                embed=make_embed(
                    "📬 Message sent",
                    f"Your message was sent to the staff of **{guild.name}**. They'll reply here — "
                    "just keep chatting in this DM to add more.",
                    discord.Color.green(),
                )
            )
        except discord.Forbidden:
            pass
    return channel


class GuildSelect(discord.ui.Select):
    def __init__(self, guilds: list[discord.Guild], message: discord.Message):
        super().__init__(
            placeholder="Choose a server…",
            options=[discord.SelectOption(label=g.name[:100], value=str(g.id)) for g in guilds[:25]],
        )
        self.first_message = message

    async def callback(self, interaction: discord.Interaction):
        guild = bot.get_guild(int(self.values[0]))
        await interaction.response.edit_message(content=f"Opening a thread with **{guild.name}**…", view=None)
        try:
            await open_thread(guild, interaction.user, self.first_message)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("Sorry, I couldn't open a thread there. Please try again later.")


class GuildPickView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], message: discord.Message):
        super().__init__(timeout=120)
        self.add_item(GuildSelect(guilds, message))


async def handle_dm(message: discord.Message) -> None:
    user = message.author
    guilds = modmail_guilds(user)
    if not guilds:
        return await message.channel.send(
            "I can't open a modmail thread for you right now. Modmail needs to be enabled in a server we share."
        )

    # Existing thread? Forward the message there.
    for g in guilds:
        channel = find_thread(g.id, user.id)
        if channel:
            await forward_to_thread(channel, message)
            await message.add_reaction("✅")
            return

    # No open thread: create one (asking which server first if there are several).
    if len(guilds) == 1:
        try:
            await open_thread(guilds[0], user, message)
        except (discord.Forbidden, discord.HTTPException):
            await message.channel.send("Sorry, I couldn't open a thread. Please try again later.")
    else:
        await message.channel.send(
            "Which server do you want to contact?", view=GuildPickView(guilds, message)
        )


async def deliver_to_user(
    user: discord.abc.User,
    guild: discord.Guild,
    staff: discord.Member,
    text: str,
    attachments: list[discord.Attachment] | None = None,
    anonymous: bool = False,
) -> discord.Embed:
    """DM the user a staff message. Returns the embed to echo in the thread. Raises discord.Forbidden."""
    dm = discord.Embed(description=text or None, color=discord.Color.green(), timestamp=discord.utils.utcnow())
    if anonymous:
        dm.set_author(name="Staff Team", icon_url=guild.icon.url if guild.icon else None)
    else:
        dm.set_author(name=staff.display_name, icon_url=staff.display_avatar.url)
    dm.set_footer(text=guild.name)
    if attachments:
        dm.add_field(name="Attachments", value="\n".join(a.url for a in attachments)[:1024], inline=False)
        image = next((a for a in attachments if (a.content_type or "").startswith("image/")), None)
        if image:
            dm.set_image(url=image.url)
    await user.send(embed=dm)

    echo = dm.copy()
    echo.set_author(name=staff.display_name, icon_url=staff.display_avatar.url)
    echo.set_footer(text="Sent anonymously" if anonymous else "Sent to user")
    return echo


async def relay_staff_message(message: discord.Message, thread: dict) -> None:
    """Send anything staff type in a modmail channel to the user. Start with // to keep a note internal."""
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if message.content.startswith("//"):
        await message.add_reaction("📝")
        return
    if not message.content and not message.attachments:
        return
    try:
        user = await bot.fetch_user(thread["user"])
        await deliver_to_user(
            user, message.guild, message.author, message.content, message.attachments,
            anonymous=thread.get("anonymous", False),
        )
    except (discord.Forbidden, discord.NotFound):
        await message.add_reaction("❌")
        await message.reply(
            "⚠️ I couldn't DM this user (they may have closed their DMs or deleted their account).",
            mention_author=False,
        )
        return
    await message.add_reaction("✅")


@bot.tree.command(name="anonymous", description="Toggle anonymous mode for this modmail thread")
@app_commands.describe(enabled="Turn on or off (leave empty to toggle)")
@app_commands.guild_only()
async def anonymous_cmd(interaction: discord.Interaction, enabled: bool | None = None):
    thread = thread_for(interaction)
    if not thread:
        return await interaction.response.send_message(
            "This command only works inside a modmail thread.", ephemeral=True
        )
    thread["anonymous"] = (not thread.get("anonymous", False)) if enabled is None else enabled
    save_data()
    if thread["anonymous"]:
        msg = "🕶️ **Anonymous mode ON** — everything sent from this thread now appears to the user as *Staff Team*."
    else:
        msg = "👤 **Anonymous mode OFF** — messages sent from this thread now show the sender's name."
    await interaction.response.send_message(msg)


@bot.tree.command(name="close", description="Close this modmail thread")
@app_commands.describe(reason="Reason shown to the user")
@app_commands.guild_only()
async def close_thread(interaction: discord.Interaction, reason: str = "No reason given"):
    thread = thread_for(interaction)
    if not thread:
        return await interaction.response.send_message(
            "This command only works inside a modmail thread.", ephemeral=True
        )
    channel = interaction.channel
    await interaction.response.send_message("🔒 Closing this thread in 5 seconds…")

    # Build a plain-text transcript of the relayed conversation
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        stamp = m.created_at.strftime("%Y-%m-%d %H:%M")
        if m.author.id == bot.user.id:
            for e in m.embeds:
                if e.author and e.author.name and (e.description or e.fields):
                    text = e.description or ""
                    for f in e.fields:
                        if f.name.startswith("Attachment"):
                            text += f" [{f.value}]"
                    lines.append(f"[{stamp}] {e.author.name}: {text}")
        elif m.content or m.attachments:
            note = "(internal note) " if m.content.startswith("//") else ""
            files = " ".join(f"[{a.url}]" for a in m.attachments)
            lines.append(f"[{stamp}] {note}{m.author.display_name}: {m.content} {files}".rstrip())
    transcript = "\n".join(lines) or "(no messages)"

    try:
        user = await bot.fetch_user(thread["user"])
    except discord.NotFound:
        user = None

    if user:
        try:
            await user.send(
                embed=make_embed(
                    "📪 Thread closed",
                    f"Your conversation with **{interaction.guild.name}** staff has been closed.\n"
                    f"**Reason:** {reason}\n\nSend me another message any time to open a new thread.",
                    discord.Color.red(),
                )
            )
        except discord.Forbidden:
            pass

    cfg = get_config(interaction.guild.id)
    log = interaction.guild.get_channel(cfg.get("modmail_log", 0))
    if log:
        who = f"{user} ({user.id})" if user else f"Unknown user ({thread['user']})"
        embed = make_embed("📪 Modmail thread closed", color=discord.Color.red())
        embed.add_field(name="User", value=who, inline=False)
        embed.add_field(name="Closed by", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason)
        file = discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{thread['user']}.txt")
        await log.send(embed=embed, file=file)

    data["modmail"].pop(str(channel.id), None)
    save_data()
    await asyncio.sleep(5)
    await channel.delete(reason=f"Modmail closed by {interaction.user}")


@bot.tree.command(name="contact", description="Start a modmail thread with a member")
@app_commands.describe(member="Who to contact", message="Your first message to them")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def contact(interaction: discord.Interaction, member: discord.Member, message: str):
    cfg = get_config(interaction.guild.id)
    if not isinstance(interaction.guild.get_channel(cfg.get("modmail_category", 0)), discord.CategoryChannel):
        return await interaction.response.send_message("Run `/modmail-setup` first.", ephemeral=True)
    if member.bot:
        return await interaction.response.send_message("You can't contact a bot.", ephemeral=True)
    existing = find_thread(interaction.guild.id, member.id)
    if existing:
        return await interaction.response.send_message(f"There's already an open thread: {existing.mention}", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        echo = await deliver_to_user(member, interaction.guild, interaction.user, message)
    except discord.Forbidden:
        return await interaction.followup.send("⚠️ I couldn't DM that member (their DMs are closed).", ephemeral=True)
    channel = await open_thread(interaction.guild, member, opened_by=interaction.user)
    await channel.send(embed=echo)
    await interaction.followup.send(f"📬 Thread opened: {channel.mention}", ephemeral=True)


@bot.tree.command(name="modmail-setup", description="Configure modmail for this server")
@app_commands.describe(
    category="Category where modmail channels are created (make it staff-only!)",
    log_channel="Channel for closed-thread transcripts",
    ping_role="Role pinged when a new thread opens",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def modmail_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    log_channel: discord.TextChannel | None = None,
    ping_role: discord.Role | None = None,
):
    cfg = get_config(interaction.guild.id)
    cfg["modmail_category"] = category.id
    cfg["modmail_log"] = log_channel.id if log_channel else 0
    cfg["modmail_role"] = ping_role.id if ping_role else 0
    save_data()
    msg = (
        f"📬 Modmail is on! New threads will appear in **{category.name}**.\n"
        "Members start a thread by DMing me."
    )
    if category.permissions_for(interaction.guild.default_role).view_channel:
        msg += "\n\n⚠️ **That category is visible to @everyone.** Edit its permissions so only staff can see it."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="modmail-block", description="Stop a user from using modmail")
@app_commands.describe(user="User to block")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def modmail_block(interaction: discord.Interaction, user: discord.User):
    blocked = get_config(interaction.guild.id).setdefault("blocked", [])
    if user.id not in blocked:
        blocked.append(user.id)
        save_data()
    await interaction.response.send_message(f"🚫 **{user}** can no longer open modmail threads.", ephemeral=True)


@bot.tree.command(name="modmail-unblock", description="Let a blocked user use modmail again")
@app_commands.describe(user="User to unblock")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def modmail_unblock(interaction: discord.Interaction, user: discord.User):
    blocked = get_config(interaction.guild.id).setdefault("blocked", [])
    if user.id in blocked:
        blocked.remove(user.id)
        save_data()
    await interaction.response.send_message(f"✅ **{user}** can use modmail again.", ephemeral=True)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    gone_modmail = data["modmail"].pop(str(channel.id), None)
    gone_ticket = data["tickets"].pop(str(channel.id), None)
    if gone_modmail or gone_ticket:
        save_data()


# ══════════════════════════════════════════════════════════════
# 📜 MOD LOG, WARNINGS & ESCALATION
# ══════════════════════════════════════════════════════════════
async def log_action(
    guild: discord.Guild,
    title: str,
    description: str | None = None,
    color: discord.Color | None = None,
    fields: list[tuple[str, object]] | None = None,
) -> None:
    """Post an embed to the mod-log channel (does nothing if none is set)."""
    channel = guild.get_channel(get_config(guild.id).get("modlog", 0))
    if not isinstance(channel, discord.TextChannel):
        return
    embed = make_embed(title, description, color or discord.Color.orange())
    embed.timestamp = discord.utils.utcnow()
    for name, value in fields or []:
        embed.add_field(name=name, value=str(value)[:1024] or "—", inline=False)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


def add_warning(guild: discord.Guild, member: discord.Member, reason: str, mod_id: int) -> int:
    warns = data["warnings"].setdefault(str(guild.id), {}).setdefault(str(member.id), [])
    warns.append({"reason": reason, "mod": mod_id, "time": datetime.now(timezone.utc).isoformat()})
    save_data()
    return len(warns)


async def apply_escalation(guild: discord.Guild, member: discord.Member, count: int) -> str | None:
    """Punish automatically when a member reaches the configured number of warnings."""
    esc = get_config(guild.id).get("escalation", {})
    ban_at, kick_at, timeout_at = esc.get("ban_at", 0), esc.get("kick_at", 0), esc.get("timeout_at", 0)
    if member == guild.owner or member.top_role >= guild.me.top_role:
        return None
    reason = f"Auto-escalation: reached {count} warnings"
    try:
        if ban_at and count >= ban_at:
            await member.ban(reason=reason)
            return f"🔨 Automatically **banned** (reached {count} warnings)."
        if kick_at and count >= kick_at:
            await member.kick(reason=reason)
            return f"👢 Automatically **kicked** (reached {count} warnings)."
        if timeout_at and count >= timeout_at:
            minutes = esc.get("timeout_minutes", 60)
            await member.timeout(timedelta(minutes=minutes), reason=reason)
            return f"🔇 Automatically **timed out** for {minutes} minute(s) (reached {count} warnings)."
    except discord.HTTPException:
        return None
    return None


@bot.tree.command(name="modlog", description="Set the moderation log channel (leave empty to turn it off)")
@app_commands.describe(channel="Where to log mod actions, deletions, edits, joins and leaves")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def modlog(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    cfg = get_config(interaction.guild.id)
    if channel is None:
        cfg["modlog"] = 0
        save_data()
        return await interaction.response.send_message("📜 Mod log turned off.", ephemeral=True)
    cfg["modlog"] = channel.id
    save_data()
    await interaction.response.send_message(f"📜 Mod log set to {channel.mention}.", ephemeral=True)
    await log_action(
        interaction.guild, "📜 Mod log enabled",
        "Kicks, bans, timeouts, warnings, automod hits, deleted/edited messages, joins and leaves will appear here.\n"
        "Make sure I have the **View Audit Log** permission.",
        discord.Color.blurple(),
    )


@bot.tree.command(name="escalation", description="Automatic punishments when a member reaches X warnings")
@app_commands.describe(
    timeout_at="Warnings before an automatic timeout (0 = off)",
    timeout_minutes="How long that timeout lasts, in minutes",
    kick_at="Warnings before an automatic kick (0 = off)",
    ban_at="Warnings before an automatic ban (0 = off)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def escalation(
    interaction: discord.Interaction,
    timeout_at: app_commands.Range[int, 0, 50] | None = None,
    timeout_minutes: app_commands.Range[int, 1, 40320] | None = None,
    kick_at: app_commands.Range[int, 0, 50] | None = None,
    ban_at: app_commands.Range[int, 0, 50] | None = None,
):
    esc = get_config(interaction.guild.id).setdefault("escalation", {})
    for key, value in (
        ("timeout_at", timeout_at), ("timeout_minutes", timeout_minutes), ("kick_at", kick_at), ("ban_at", ban_at)
    ):
        if value is not None:
            esc[key] = value
    save_data()

    def show(n: int) -> str:
        return f"at **{n}** warnings" if n else "off"

    embed = make_embed("⚖️ Warning escalation")
    embed.add_field(name="Timeout", value=f"{show(esc.get('timeout_at', 0))} → {esc.get('timeout_minutes', 60)} min")
    embed.add_field(name="Kick", value=show(esc.get("kick_at", 0)))
    embed.add_field(name="Ban", value=show(esc.get("ban_at", 0)))
    embed.set_footer(text="Applies to /warn and automod strikes. Run with no options to just view.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unwarn", description="Remove one warning from a member")
@app_commands.describe(member="Member", number="Warning number as shown by /warnings")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def unwarn(interaction: discord.Interaction, member: discord.Member, number: app_commands.Range[int, 1, 1000]):
    warns = data["warnings"].get(str(interaction.guild.id), {}).get(str(member.id), [])
    if number > len(warns):
        return await interaction.response.send_message(
            f"{member} only has {len(warns)} warning(s).", ephemeral=True
        )
    removed = warns.pop(number - 1)
    save_data()
    await interaction.response.send_message(f"🧽 Removed warning #{number} from **{member}** ({removed['reason']}).")
    await log_action(
        interaction.guild, "🧽 Warning removed", f"{member.mention} — by {interaction.user.mention}",
        discord.Color.green(), [("Removed warning", removed["reason"])],
    )


last_deleted: dict[int, dict] = {}
_silent_deletes: set[int] = set()  # message IDs the bot deleted itself (don't log twice)


def _in_modmail_thread(channel_id: int) -> bool:
    return str(channel_id) in data["modmail"]


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.id in _silent_deletes:
        _silent_deletes.discard(message.id)
        return
    if _in_modmail_thread(message.channel.id):
        return
    last_deleted[message.channel.id] = {
        "content": message.content, "author": str(message.author),
        "avatar": message.author.display_avatar.url, "time": discord.utils.utcnow(),
    }
    await log_action(
        message.guild, "🗑️ Message deleted",
        f"By {message.author.mention} in {message.channel.mention}",
        discord.Color.red(), [("Content", message.content[:1000] or "(no text)")],
    )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot or not after.guild or before.content == after.content:
        return
    if _in_modmail_thread(after.channel.id):
        return
    if await run_automod(after, check_spam=False):  # stop people editing a bad message in
        return
    await log_action(
        after.guild, "✏️ Message edited",
        f"By {after.author.mention} in {after.channel.mention} — [jump]({after.jump_url})",
        discord.Color.blue(),
        [("Before", before.content[:1000] or "(no text)"), ("After", after.content[:1000] or "(no text)")],
    )


_quiet_leaves: set[int] = set()  # members removed by anti-raid (don't spam the log)


@bot.event
async def on_member_remove(member: discord.Member):
    await store_sticky_roles(member)
    if member.id in _quiet_leaves:
        _quiet_leaves.discard(member.id)
        return
    fields = []
    if member.joined_at:
        fields.append(("Joined", discord.utils.format_dt(member.joined_at, "R")))
    await log_action(
        member.guild, "📤 Member left", f"{member.mention} (`{member.id}`)", discord.Color.dark_grey(), fields
    )


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    """Logs kicks, bans, unbans and timeouts no matter who did them (needs View Audit Log)."""
    A = discord.AuditLogAction
    if entry.action == A.ban:
        forget_sticky(entry.guild.id, entry.target_id)
    if entry.reason and entry.reason.startswith(("Antiraid:", "Honeypot:")):
        return
    fields: list[tuple[str, object]] = []
    if entry.action == A.kick:
        title, color = "👢 Member kicked", discord.Color.orange()
    elif entry.action == A.ban:
        title, color = "🔨 Member banned", discord.Color.red()
    elif entry.action == A.unban:
        title, color = "✅ Member unbanned", discord.Color.green()
    elif entry.action == A.member_update:
        before = getattr(entry.before, "timed_out_until", None)
        after = getattr(entry.after, "timed_out_until", None)
        if before is None and after is None:
            return
        if after and after > discord.utils.utcnow():
            title, color = "🔇 Member timed out", discord.Color.orange()
            fields.append(("Until", discord.utils.format_dt(after, "f")))
        else:
            title, color = "🔊 Timeout removed", discord.Color.green()
    else:
        return
    mod = entry.user.mention if entry.user else "Unknown"
    fields.append(("Reason", entry.reason or "No reason given"))
    await log_action(entry.guild, title, f"<@{entry.target_id}> (`{entry.target_id}`)\n**By:** {mod}", color, fields)


@bot.tree.command(name="snipe", description="Show the last deleted message in this channel (staff only)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def snipe(interaction: discord.Interaction):
    d = last_deleted.get(interaction.channel_id)
    if not d:
        return await interaction.response.send_message("Nothing to snipe here.", ephemeral=True)
    embed = make_embed("🗑️ Last deleted message", d["content"] or "*(no text)*")
    embed.set_author(name=d["author"], icon_url=d["avatar"])
    embed.timestamp = d["time"]
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="Report message")
@app_commands.guild_only()
async def report_message(interaction: discord.Interaction, message: discord.Message):
    channel = interaction.guild.get_channel(get_config(interaction.guild.id).get("modlog", 0))
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message(
            "Reporting isn't set up on this server yet.", ephemeral=True
        )
    await log_action(
        interaction.guild, "🚩 Message reported",
        f"Reported by {interaction.user.mention}\n**Author:** {message.author.mention} in {message.channel.mention}\n"
        f"[Jump to message]({message.jump_url})",
        discord.Color.red(), [("Content", message.content[:1000] or "(no text)")],
    )
    await interaction.response.send_message("✅ Thanks — the staff team has been notified.", ephemeral=True)


# ══════════════════════════════════════════════════════════════
# 🤖 AUTOMOD
# ══════════════════════════════════════════════════════════════
AUTOMOD_DEFAULTS = {
    "enabled": False,
    "strikes": True,  # each violation also adds a warning
    "spam": {"on": False, "messages": 5, "seconds": 5},
    "invites": {"on": False},
    "links": {"on": False, "allowed": []},
    "badwords": {"on": False, "words": []},
    "mentions": {"on": False, "limit": 5},
    "caps": {"on": False, "percent": 70, "min_length": 12},
    "exempt_roles": [],
    "exempt_channels": [],
}
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)
URL_RE = re.compile(r"https?://([^\s/<>]+)", re.I)
ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
spam_tracker: dict[tuple[int, int], deque] = defaultdict(deque)


def automod_cfg(guild_id: int) -> dict:
    cfg = get_config(guild_id).setdefault("automod", {})
    for key, default in AUTOMOD_DEFAULTS.items():
        if isinstance(default, dict):
            sub = cfg.setdefault(key, {})
            for k, v in default.items():
                sub.setdefault(k, copy.deepcopy(v))
        else:
            cfg.setdefault(key, copy.deepcopy(default))
    return cfg


def automod_violation(content: str, mention_count: int, cfg: dict) -> str | None:
    """Return the reason a message breaks a (non-spam) rule, or None."""
    text = ZERO_WIDTH_RE.sub("", content)
    if cfg["invites"]["on"] and INVITE_RE.search(text):
        return "Invite links aren't allowed"
    if cfg["links"]["on"]:
        for domain in URL_RE.findall(text):
            d = domain.lower().split("@")[-1].split(":")[0]
            d = d[4:] if d.startswith("www.") else d
            if not any(d == a or d.endswith("." + a) for a in cfg["links"]["allowed"]):
                return "Links aren't allowed here"
    words = cfg["badwords"]["words"]
    if cfg["badwords"]["on"] and words:
        pattern = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        if re.search(rf"(?<!\w)(?:{pattern})(?!\w)", text, re.I):
            return "Watch your language"
    if cfg["mentions"]["on"] and mention_count > cfg["mentions"]["limit"]:
        return "Too many mentions"
    if cfg["caps"]["on"]:
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= cfg["caps"]["min_length"]:
            if sum(c.isupper() for c in letters) / len(letters) * 100 >= cfg["caps"]["percent"]:
                return "Excessive caps"
    return None


def track_spam(message: discord.Message, cfg: dict) -> list[discord.Message] | None:
    s = cfg["spam"]
    if not s["on"]:
        return None
    dq = spam_tracker[(message.guild.id, message.author.id)]
    now = time.time()
    dq.append((now, message))
    while dq and now - dq[0][0] > s["seconds"]:
        dq.popleft()
    if len(dq) >= s["messages"]:
        msgs = [m for _, m in dq]
        dq.clear()
        return msgs
    return None


async def run_automod(message: discord.Message, check_spam: bool = True) -> bool:
    """Returns True if the message was removed by automod."""
    member = message.author
    if not isinstance(member, discord.Member) or not message.guild:
        return False
    cfg = automod_cfg(message.guild.id)
    if not cfg["enabled"]:
        return False
    if member.guild_permissions.manage_messages:  # moderators are exempt
        return False
    if message.channel.id in cfg["exempt_channels"] or any(r.id in cfg["exempt_roles"] for r in member.roles):
        return False

    mentions = len(set(message.mentions)) + len(message.role_mentions) + (1 if message.mention_everyone else 0)
    reason = automod_violation(message.content, mentions, cfg)
    to_delete = [message]
    if reason is None and check_spam:
        spam = track_spam(message, cfg)
        if spam:
            reason, to_delete = "Spamming", spam
    if reason is None:
        return False

    for m in to_delete:
        _silent_deletes.add(m.id)
        try:
            await m.delete()
        except (discord.NotFound, discord.Forbidden):
            _silent_deletes.discard(m.id)

    extra, action = "", None
    if cfg["strikes"]:
        count = add_warning(message.guild, member, f"Automod: {reason}", bot.user.id)
        action = await apply_escalation(message.guild, member, count)
        extra = f" (warning **{count}**)"
    try:
        await message.channel.send(
            f"🛡️ {member.mention}, your message was removed: **{reason}**{extra}",
            delete_after=8,
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
    except discord.HTTPException:
        pass
    fields = [("Rule", reason), ("Content", message.content[:900] or "(no text)")]
    if action:
        fields.append(("Escalation", action))
    await log_action(
        message.guild, "🛡️ Automod",
        f"{member.mention} (`{member.id}`) in {message.channel.mention}", discord.Color.orange(), fields,
    )
    return True


automod = app_commands.Group(
    name="automod",
    description="Configure auto-moderation",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)
manage_guild_only = app_commands.checks.has_permissions(administrator=True)
RULE_CHOICES = [
    app_commands.Choice(name="Spam", value="spam"),
    app_commands.Choice(name="Discord invites", value="invites"),
    app_commands.Choice(name="Links", value="links"),
    app_commands.Choice(name="Bad words", value="badwords"),
    app_commands.Choice(name="Mass mentions", value="mentions"),
    app_commands.Choice(name="Excessive caps", value="caps"),
]
LIST_ACTIONS = [
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
    app_commands.Choice(name="List", value="list"),
]


def onoff(value: bool) -> str:
    return "✅" if value else "❌"


def automod_embed(guild_id: int) -> discord.Embed:
    c = automod_cfg(guild_id)
    embed = make_embed(
        "🤖 Auto-moderation",
        f"Master switch: {onoff(c['enabled'])}   ·   Strikes (warnings): {onoff(c['strikes'])}\n"
        "Moderators (Manage Messages) are always exempt.",
    )
    embed.add_field(name="Spam", value=f"{onoff(c['spam']['on'])} {c['spam']['messages']} msgs / {c['spam']['seconds']}s")
    embed.add_field(name="Invites", value=onoff(c["invites"]["on"]))
    embed.add_field(name="Links", value=f"{onoff(c['links']['on'])} ({len(c['links']['allowed'])} allowed domains)")
    embed.add_field(name="Bad words", value=f"{onoff(c['badwords']['on'])} ({len(c['badwords']['words'])} words)")
    embed.add_field(name="Mentions", value=f"{onoff(c['mentions']['on'])} max {c['mentions']['limit']}")
    embed.add_field(name="Caps", value=f"{onoff(c['caps']['on'])} ≥{c['caps']['percent']}% of {c['caps']['min_length']}+ letters")
    roles = " ".join(f"<@&{r}>" for r in c["exempt_roles"]) or "none"
    chans = " ".join(f"<#{ch}>" for ch in c["exempt_channels"]) or "none"
    embed.add_field(name="Exempt roles", value=roles, inline=False)
    embed.add_field(name="Exempt channels", value=chans, inline=False)
    return embed


@automod.command(name="status", description="Show the current auto-moderation settings")
@manage_guild_only
async def automod_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=automod_embed(interaction.guild.id), ephemeral=True)


@automod.command(name="toggle", description="Turn auto-moderation on or off")
@app_commands.describe(enabled="Master switch")
@manage_guild_only
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    automod_cfg(interaction.guild.id)["enabled"] = enabled
    save_data()
    hint = "\nTurn on individual rules with `/automod rule`." if enabled else ""
    await interaction.response.send_message(
        f"🤖 Auto-moderation is now **{'ON' if enabled else 'OFF'}**.{hint}", ephemeral=True
    )


@automod.command(name="rule", description="Turn an individual rule on or off")
@app_commands.describe(rule="Which rule", enabled="On or off")
@app_commands.choices(rule=RULE_CHOICES)
@manage_guild_only
async def automod_rule(interaction: discord.Interaction, rule: app_commands.Choice[str], enabled: bool):
    c = automod_cfg(interaction.guild.id)
    c[rule.value]["on"] = enabled
    save_data()
    msg = f"{onoff(enabled)} **{rule.name}** rule {'enabled' if enabled else 'disabled'}."
    if not c["enabled"]:
        msg += "\n⚠️ The master switch is off — run `/automod toggle enabled:True`."
    if rule.value == "badwords" and enabled and not c["badwords"]["words"]:
        msg += "\nAdd words with `/automod badword`."
    await interaction.response.send_message(msg, ephemeral=True)


@automod.command(name="spam", description="Set spam detection thresholds")
@app_commands.describe(messages="Messages that count as spam", seconds="…sent within this many seconds")
@manage_guild_only
async def automod_spam(
    interaction: discord.Interaction, messages: app_commands.Range[int, 2, 20], seconds: app_commands.Range[int, 2, 60]
):
    c = automod_cfg(interaction.guild.id)["spam"]
    c["messages"], c["seconds"] = messages, seconds
    save_data()
    await interaction.response.send_message(f"Spam = **{messages}** messages in **{seconds}s**.", ephemeral=True)


@automod.command(name="mentions", description="Set the maximum mentions allowed per message")
@app_commands.describe(limit="Max user/role mentions in one message")
@manage_guild_only
async def automod_mentions(interaction: discord.Interaction, limit: app_commands.Range[int, 2, 50]):
    automod_cfg(interaction.guild.id)["mentions"]["limit"] = limit
    save_data()
    await interaction.response.send_message(f"Max mentions per message: **{limit}**.", ephemeral=True)


@automod.command(name="caps", description="Set the caps filter sensitivity")
@app_commands.describe(percent="Percent of letters in CAPS that triggers it", min_length="Ignore messages shorter than this many letters")
@manage_guild_only
async def automod_caps(
    interaction: discord.Interaction,
    percent: app_commands.Range[int, 30, 100],
    min_length: app_commands.Range[int, 5, 200] = 12,
):
    c = automod_cfg(interaction.guild.id)["caps"]
    c["percent"], c["min_length"] = percent, min_length
    save_data()
    await interaction.response.send_message(
        f"Caps filter: **{percent}%** of at least **{min_length}** letters.", ephemeral=True
    )


@automod.command(name="badword", description="Manage the blocked word list")
@app_commands.describe(action="Add, remove or list", word="Word or phrase (not needed for list)")
@app_commands.choices(action=LIST_ACTIONS)
@manage_guild_only
async def automod_badword(interaction: discord.Interaction, action: app_commands.Choice[str], word: str | None = None):
    words = automod_cfg(interaction.guild.id)["badwords"]["words"]
    if action.value == "list":
        text = ", ".join(f"`{w}`" for w in words) or "The list is empty."
        return await interaction.response.send_message(text[:1900], ephemeral=True)
    if not word or not word.strip():
        return await interaction.response.send_message("Tell me which word.", ephemeral=True)
    w = word.lower().strip()[:50]
    if action.value == "add":
        if w in words:
            return await interaction.response.send_message("That word is already on the list.", ephemeral=True)
        if len(words) >= 200:
            return await interaction.response.send_message("The list is full (200 words).", ephemeral=True)
        words.append(w)
        msg = f"Added `{w}` ({len(words)} total)."
    else:
        if w not in words:
            return await interaction.response.send_message("That word isn't on the list.", ephemeral=True)
        words.remove(w)
        msg = f"Removed `{w}` ({len(words)} total)."
    save_data()
    await interaction.response.send_message(msg, ephemeral=True)


@automod.command(name="linkdomain", description="Manage domains allowed by the links rule")
@app_commands.describe(action="Add, remove or list", domain="e.g. youtube.com (not needed for list)")
@app_commands.choices(action=LIST_ACTIONS)
@manage_guild_only
async def automod_linkdomain(interaction: discord.Interaction, action: app_commands.Choice[str], domain: str | None = None):
    allowed = automod_cfg(interaction.guild.id)["links"]["allowed"]
    if action.value == "list":
        text = ", ".join(f"`{d}`" for d in allowed) or "No domains are allowed."
        return await interaction.response.send_message(text[:1900], ephemeral=True)
    if not domain or not domain.strip():
        return await interaction.response.send_message("Tell me which domain.", ephemeral=True)
    d = re.sub(r"^https?://", "", domain.lower().strip()).split("/")[0]
    d = d[4:] if d.startswith("www.") else d
    if action.value == "add":
        if d not in allowed:
            allowed.append(d)
        msg = f"Links to `{d}` (and its subdomains) are now allowed."
    else:
        if d in allowed:
            allowed.remove(d)
        msg = f"Removed `{d}` from the allowed domains."
    save_data()
    await interaction.response.send_message(msg, ephemeral=True)


@automod.command(name="exempt", description="Toggle a role or channel as exempt from automod")
@app_commands.describe(role="Role to exempt (run again to remove)", channel="Channel to exempt (run again to remove)")
@manage_guild_only
async def automod_exempt(
    interaction: discord.Interaction, role: discord.Role | None = None, channel: discord.TextChannel | None = None
):
    if role is None and channel is None:
        return await interaction.response.send_message("Pick a role and/or a channel.", ephemeral=True)
    c = automod_cfg(interaction.guild.id)
    notes = []
    for obj, key, label in ((role, "exempt_roles", "role"), (channel, "exempt_channels", "channel")):
        if obj is None:
            continue
        if obj.id in c[key]:
            c[key].remove(obj.id)
            notes.append(f"Removed {label} **{obj.name}** from the exempt list.")
        else:
            c[key].append(obj.id)
            notes.append(f"**{obj.name}** is now exempt ({label}).")
    save_data()
    await interaction.response.send_message("\n".join(notes), ephemeral=True)


@automod.command(name="strikes", description="Choose whether automod violations also add a warning")
@app_commands.describe(enabled="If on, violations count toward /escalation")
@manage_guild_only
async def automod_strikes(interaction: discord.Interaction, enabled: bool):
    automod_cfg(interaction.guild.id)["strikes"] = enabled
    save_data()
    await interaction.response.send_message(
        f"Automod strikes are now **{'ON' if enabled else 'OFF'}**.", ephemeral=True
    )


bot.tree.add_command(automod)


AUTOMOD_PRESETS = {
    "light": {
        "label": "Light",
        "rules": {"spam": False, "invites": True, "links": False, "mentions": True, "caps": False},
        "spam": (6, 5), "mentions": 8, "caps": (80, 15),
        "strikes": False,
        "escalation": (0, 60, 0, 0),   # (timeout_at, timeout_minutes, kick_at, ban_at)
        "antiraid": (0, False),        # (min_account_age_days, detect_floods)
    },
    "standard": {
        "label": "Standard",
        "rules": {"spam": True, "invites": True, "links": False, "mentions": True, "caps": True},
        "spam": (5, 5), "mentions": 5, "caps": (70, 12),
        "strikes": True,
        "escalation": (3, 60, 6, 0),
        "antiraid": (0, True),
    },
    "strict": {
        "label": "Strict",
        "rules": {"spam": True, "invites": True, "links": True, "mentions": True, "caps": True},
        "spam": (4, 5), "mentions": 4, "caps": (60, 10),
        "strikes": True,
        "escalation": (2, 120, 4, 7),
        "antiraid": (3, True),
    },
}
BOT_PERMS_NEEDED = {
    "manage_messages": "Manage Messages",
    "moderate_members": "Moderate Members",
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "view_audit_log": "View Audit Log",
}


@bot.tree.command(name="automod-setup", description="One-step auto-moderation setup using a Light / Standard / Strict preset")
@app_commands.describe(
    preset="How strict to be",
    log_channel="Channel for the mod log (optional)",
    exempt_role="A trusted role that automod should ignore (optional)",
)
@app_commands.choices(preset=[
    app_commands.Choice(name="Light — invites & mass mentions, no punishments", value="light"),
    app_commands.Choice(name="Standard — spam, invites, mentions, caps + escalation", value="standard"),
    app_commands.Choice(name="Strict — everything, links blocked, harsh escalation", value="strict"),
])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def automod_setup(
    interaction: discord.Interaction,
    preset: app_commands.Choice[str],
    log_channel: discord.TextChannel | None = None,
    exempt_role: discord.Role | None = None,
):
    p = AUTOMOD_PRESETS[preset.value]
    guild = interaction.guild
    c = automod_cfg(guild.id)
    cfg = get_config(guild.id)

    # Rules and thresholds (your bad-word list, allowed domains and exemptions are kept)
    c["enabled"] = True
    c["strikes"] = p["strikes"]
    for rule, state in p["rules"].items():
        c[rule]["on"] = state
    c["badwords"]["on"] = bool(c["badwords"]["words"]) and preset.value != "light"
    c["spam"]["messages"], c["spam"]["seconds"] = p["spam"]
    c["mentions"]["limit"] = p["mentions"]
    c["caps"]["percent"], c["caps"]["min_length"] = p["caps"]

    timeout_at, timeout_minutes, kick_at, ban_at = p["escalation"]
    cfg["escalation"] = {"timeout_at": timeout_at, "timeout_minutes": timeout_minutes, "kick_at": kick_at, "ban_at": ban_at}
    raid = cfg.setdefault("antiraid", {})
    raid["min_age_days"], raid["detect"] = p["antiraid"]
    raid.setdefault("joins", 5)
    raid.setdefault("seconds", 10)

    if log_channel:
        cfg["modlog"] = log_channel.id
    if exempt_role and exempt_role.id not in c["exempt_roles"]:
        c["exempt_roles"].append(exempt_role.id)
    save_data()

    # Summary
    active = [r for r in ("spam", "invites", "links", "badwords", "mentions", "caps") if c[r]["on"]]
    steps = []
    if timeout_at:
        steps.append(f"{timeout_at} warnings → timeout {timeout_minutes} min")
    if kick_at:
        steps.append(f"{kick_at} → kick")
    if ban_at:
        steps.append(f"{ban_at} → ban")
    raid_bits = []
    if raid["min_age_days"]:
        raid_bits.append(f"accounts under {raid['min_age_days']} days old are kicked")
    if raid["detect"]:
        raid_bits.append(f"raid detection ({raid['joins']} joins / {raid['seconds']}s)")
    modlog_ch = guild.get_channel(cfg.get("modlog", 0))

    embed = make_embed(f"🤖 Auto-moderation set up — {p['label']}", color=discord.Color.green())
    embed.add_field(name="Active rules", value=", ".join(active) or "none", inline=False)
    embed.add_field(name="Strikes", value="Violations add a warning" if p["strikes"] else "Delete only, no warnings")
    embed.add_field(name="Escalation", value="; ".join(steps) or "none")
    embed.add_field(name="Anti-raid", value="; ".join(raid_bits) or "off", inline=False)
    embed.add_field(name="Mod log", value=modlog_ch.mention if modlog_ch else "⚠️ not set — run `/modlog`", inline=False)

    tips = []
    if c["links"]["on"] and not c["links"]["allowed"]:
        tips.append("Links are blocked everywhere — allow trusted sites with `/automod linkdomain`.")
    if not c["badwords"]["words"]:
        tips.append("Add words to filter with `/automod badword`.")
    tips.append("Exempt roles/channels with `/automod exempt`. Fine-tune anything with `/automod status`.")
    embed.add_field(name="Next steps", value="\n".join(f"• {t}" for t in tips), inline=False)

    perms = guild.me.guild_permissions
    missing = [label for attr, label in BOT_PERMS_NEEDED.items() if not getattr(perms, attr)]
    if missing:
        embed.add_field(name="⚠️ I'm missing permissions", value=", ".join(missing) + "\nGive them to my role or some features won't work.", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)
    await log_action(
        guild, "🤖 Auto-moderation set up", f"Preset **{p['label']}** applied by {interaction.user.mention}.",
        discord.Color.blurple(),
    )


# ══════════════════════════════════════════════════════════════
# 🚨 ANTI-RAID
# ══════════════════════════════════════════════════════════════
join_log: dict[int, deque] = defaultdict(deque)


async def _remove_joiner(member: discord.Member, dm_text: str, why: str, log: bool = True) -> None:
    _quiet_leaves.add(member.id)
    try:
        await member.send(dm_text)
    except discord.HTTPException:
        pass
    try:
        await member.kick(reason=f"Antiraid: {why}")
    except discord.HTTPException:
        _quiet_leaves.discard(member.id)
        return
    if log:
        await log_action(
            member.guild, "🚨 Anti-raid", f"Removed {member.mention} (`{member.id}`) — {why}", discord.Color.red()
        )


async def antiraid_check(member: discord.Member) -> bool:
    """Returns True if the new member was removed."""
    if member.bot:
        return False
    guild = member.guild
    cfg = get_config(guild.id).get("antiraid", {})
    if not cfg:
        return False
    now = time.time()

    min_days = cfg.get("min_age_days", 0)
    if min_days and discord.utils.utcnow() - member.created_at < timedelta(days=min_days):
        await _remove_joiner(
            member, f"Your account must be at least {min_days} day(s) old to join **{guild.name}**.",
            f"account younger than {min_days} day(s)",
        )
        return True

    if now < cfg.get("raid_until", 0):
        await _remove_joiner(
            member, f"**{guild.name}** is under raid protection right now. Please try again later.", "raid mode"
        )
        return True

    if cfg.get("detect"):
        window = cfg.get("seconds", 10)
        dq = join_log[guild.id]
        dq.append((now, member))
        while dq and now - dq[0][0] > window:
            dq.popleft()
        if len(dq) >= cfg.get("joins", 5):
            cfg["raid_until"] = now + 600
            save_data()
            recent = [m for _, m in dq]
            dq.clear()
            await log_action(
                guild, "🚨 Raid detected",
                f"{len(recent)} members joined within {window}s.\nRaid mode is on for **10 minutes** — new joins are kicked. "
                "Use `/raidmode enabled:False` to end it early.",
                discord.Color.red(),
            )
            for m in recent:
                await _remove_joiner(m, f"**{guild.name}** is under raid protection right now. Please try again later.",
                                     "raid detected", log=False)
            return True
    return False


@bot.tree.command(name="antiraid", description="Configure join protection (run with no options to view)")
@app_commands.describe(
    min_account_age="Kick joins whose account is younger than this many days (0 = off)",
    detection="Automatically detect join floods",
    joins="Joins within the time window that count as a raid",
    seconds="Length of that time window",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def antiraid(
    interaction: discord.Interaction,
    min_account_age: app_commands.Range[int, 0, 365] | None = None,
    detection: bool | None = None,
    joins: app_commands.Range[int, 2, 50] | None = None,
    seconds: app_commands.Range[int, 3, 120] | None = None,
):
    cfg = get_config(interaction.guild.id).setdefault("antiraid", {})
    for key, value in (("min_age_days", min_account_age), ("detect", detection), ("joins", joins), ("seconds", seconds)):
        if value is not None:
            cfg[key] = value
    save_data()
    age = cfg.get("min_age_days", 0)
    embed = make_embed("🚨 Anti-raid")
    embed.add_field(name="Minimum account age", value=f"{age} day(s)" if age else "off")
    embed.add_field(name="Raid detection", value=f"{onoff(cfg.get('detect', False))} {cfg.get('joins', 5)} joins / {cfg.get('seconds', 10)}s")
    left = int(cfg.get("raid_until", 0) - time.time())
    embed.add_field(name="Raid mode", value=f"🔴 active ({left}s left)" if left > 0 else "off")
    embed.set_footer(text="I need the Kick Members permission for this to work.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="raidmode", description="Manually turn raid mode on (new joins are kicked) or off")
@app_commands.describe(enabled="On or off", minutes="How long to keep it on (default 10)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def raidmode(interaction: discord.Interaction, enabled: bool, minutes: app_commands.Range[int, 1, 1440] = 10):
    cfg = get_config(interaction.guild.id).setdefault("antiraid", {})
    cfg["raid_until"] = time.time() + minutes * 60 if enabled else 0
    save_data()
    msg = f"🚨 Raid mode **ON** for {minutes} minute(s). New joins will be kicked." if enabled else "✅ Raid mode **OFF**."
    await interaction.response.send_message(msg)
    await log_action(
        interaction.guild, "🚨 Raid mode " + ("enabled" if enabled else "disabled"),
        f"By {interaction.user.mention}", discord.Color.red() if enabled else discord.Color.green(),
    )


# ══════════════════════════════════════════════════════════════
# 🔧 MORE MODERATION
# ══════════════════════════════════════════════════════════════
_DURATION_PART = re.compile(r"(\d+)\s*([smhdw])", re.I)
_DURATION_FULL = re.compile(r"(?:\d+\s*[smhdw]\s*)+", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    """'3d', '12h', '1w2d', '90m' → timedelta (max 1 year)."""
    text = text.strip()
    if not _DURATION_FULL.fullmatch(text):
        return None
    seconds = sum(int(n) * _UNITS[u.lower()] for n, u in _DURATION_PART.findall(text))
    if seconds <= 0 or seconds > 365 * 86400:
        return None
    return timedelta(seconds=seconds)


@tasks.loop(seconds=60)
async def tempban_loop():
    now = time.time()
    for key, until in list(data["tempbans"].items()):
        if now < until:
            continue
        gid, uid = (int(x) for x in key.split(":"))
        guild = bot.get_guild(gid)
        if guild is None:
            continue
        try:
            await guild.unban(discord.Object(id=uid), reason="Tempban expired")
        except discord.NotFound:
            pass  # already unbanned
        except discord.HTTPException:
            continue  # try again next minute
        data["tempbans"].pop(key, None)
        save_data()


@tempban_loop.before_loop
async def _before_tempban_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="tempban", description="Ban a member for a set time, then unban automatically")
@app_commands.describe(member="Member to ban", duration="e.g. 30m, 12h, 3d, 1w2d", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def tempban(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason given"):
    delta = parse_duration(duration)
    if delta is None or delta < timedelta(minutes=1):
        return await interaction.response.send_message(
            "I couldn't read that duration. Try `30m`, `12h`, `3d` or `1w2d` (1 minute – 1 year).", ephemeral=True
        )
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    try:
        await member.send(
            f"You've been temporarily banned from **{interaction.guild.name}** for **{duration}**.\nReason: {reason}"
        )
    except discord.HTTPException:
        pass
    await member.ban(reason=f"{interaction.user}: {reason} (tempban {duration})")
    data["tempbans"][f"{interaction.guild.id}:{member.id}"] = time.time() + delta.total_seconds()
    save_data()
    ends = discord.utils.format_dt(discord.utils.utcnow() + delta, "R")
    await interaction.response.send_message(f"⏳ Temporarily banned **{member}** — unbanned {ends}. ({reason})")


@bot.tree.command(name="softban", description="Ban and instantly unban to wipe a member's recent messages")
@app_commands.describe(member="Member to softban", reason="Reason")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def softban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction.response.defer()
    await member.ban(reason=f"{interaction.user}: softban — {reason}", delete_message_seconds=86400)
    await interaction.guild.unban(member, reason="Softban")
    await interaction.followup.send(f"🧹 Softbanned **{member}** (last 24h of messages deleted) — {reason}")


@bot.tree.command(name="purge-user", description="Delete one member's recent messages in this channel")
@app_commands.describe(member="Whose messages to delete", scan="How many recent messages to scan (max 500)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def purge_user(interaction: discord.Interaction, member: discord.Member, scan: app_commands.Range[int, 1, 500] = 100):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=scan, check=lambda m: m.author.id == member.id)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s) from {member.mention}.", ephemeral=True)


# ══════════════════════════════════════════════════════════════
# 🎭 AUTOROLE & ROLE MENUS
# ══════════════════════════════════════════════════════════════
def is_dangerous_role(role: discord.Role) -> bool:
    p = role.permissions
    return any((p.administrator, p.manage_guild, p.manage_roles, p.manage_channels, p.kick_members,
                p.ban_members, p.moderate_members, p.manage_messages))


@bot.tree.command(name="autorole", description="Give every new member a role when they join (leave empty to turn off)")
@app_commands.describe(role="Role to assign automatically")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def autorole(interaction: discord.Interaction, role: discord.Role | None = None):
    cfg = get_config(interaction.guild.id)
    if role is None:
        cfg["autorole"] = 0
        save_data()
        return await interaction.response.send_message("Autorole turned off.", ephemeral=True)
    if err := role_error(interaction, role):
        return await interaction.response.send_message(err, ephemeral=True)
    if role.is_default() or role.managed or is_dangerous_role(role):
        return await interaction.response.send_message(
            "That role can't be used as an autorole (it's managed, @everyone, or has moderator permissions).",
            ephemeral=True,
        )
    cfg["autorole"] = role.id
    save_data()
    await interaction.response.send_message(
        f"New members will now get **{role.name}**.\n"
        "Note: if you also use verification, an autorole is given *before* they verify.",
        ephemeral=True,
    )


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolemenu:(?P<id>[0-9]+)"):
    def __init__(self, role_id: int, label: str = "Role"):
        super().__init__(
            discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"rolemenu:{role_id}")
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(int(match["id"]), item.label or "Role")

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None or role.is_default() or role.managed or is_dangerous_role(role):
            return await interaction.response.send_message("That role isn't available anymore.", ephemeral=True)
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role menu")
                msg = f"➖ Removed **{role.name}**."
            else:
                await member.add_roles(role, reason="Role menu")
                msg = f"➕ You now have **{role.name}**."
        except discord.Forbidden:
            msg = "I can't manage that role — ask a moderator to move my role above it."
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="rolemenu", description="Post a panel where members pick roles with buttons")
@app_commands.describe(
    channel="Where to post the panel", title="Panel title", role1="First role",
    role2="Second role", role3="Third role", role4="Fourth role", role5="Fifth role",
    description="Text shown on the panel",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def rolemenu(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    role1: discord.Role,
    role2: discord.Role | None = None,
    role3: discord.Role | None = None,
    role4: discord.Role | None = None,
    role5: discord.Role | None = None,
    description: str = "Click a button to add or remove a role.",
):
    roles = []
    for r in (role1, role2, role3, role4, role5):
        if r is None or r in roles:
            continue
        if err := role_error(interaction, r):
            return await interaction.response.send_message(f"**{r.name}**: {err}", ephemeral=True)
        if r.is_default() or r.managed or is_dangerous_role(r):
            return await interaction.response.send_message(
                f"**{r.name}** can't go on a role menu (managed, @everyone, or has moderator permissions).",
                ephemeral=True,
            )
        roles.append(r)
    view = discord.ui.View(timeout=None)
    for r in roles:
        view.add_item(RoleButton(r.id, r.name[:80]))
    await channel.send(embed=make_embed(f"🎭 {title}", description), view=view)
    await interaction.response.send_message(f"Role menu posted in {channel.mention}.", ephemeral=True)


# ══════════════════════════════════════════════════════════════
# 🔁 STICKY ROLES
# When someone leaves, remember their roles; give them back if they rejoin.
# ══════════════════════════════════════════════════════════════
STICKY_EXPIRY = 60 * 86400  # forget people who stay away for 60 days
_last_sticky_prune: dict[int, float] = {}


def sticky_cfg(guild_id: int) -> dict:
    cfg = get_config(guild_id).setdefault("sticky", {})
    cfg.setdefault("enabled", False)
    cfg.setdefault("roles", [])  # optional allow-list; empty = every "safe" role
    return cfg


def forget_sticky(guild_id: int, user_id: int) -> bool:
    if data["sticky"].get(str(guild_id), {}).pop(str(user_id), None) is not None:
        save_data()
        return True
    return False


def sticky_roles_to_restore(guild: discord.Guild, role_ids: list[int], allow: list[int]) -> list[discord.Role]:
    """Which of the remembered roles can (and should) be handed back right now."""
    unverified_id = get_config(guild.id).get("unverified_role", 0)
    roles = []
    for rid in role_ids:
        role = guild.get_role(rid)
        if role is None or rid == unverified_id or role.is_default() or role.managed:
            continue
        if role >= guild.me.top_role:  # I can't hand out roles above my own
            continue
        if allow:
            if role.id not in allow:
                continue
        elif is_dangerous_role(role):  # with no allow-list, never auto-restore moderator roles
            continue
        roles.append(role)
    return roles


def _prune_sticky(guild_id: int) -> None:
    now = time.time()
    if now - _last_sticky_prune.get(guild_id, 0) < 3600:
        return
    _last_sticky_prune[guild_id] = now
    store = data["sticky"].get(str(guild_id), {})
    for uid, entry in list(store.items()):
        if now - entry.get("time", 0) > STICKY_EXPIRY:
            store.pop(uid, None)


async def store_sticky_roles(member: discord.Member) -> None:
    if member.bot or not sticky_cfg(member.guild.id)["enabled"]:
        return
    unverified_id = get_config(member.guild.id).get("unverified_role", 0)
    ids = [r.id for r in member.roles if not r.is_default() and not r.managed and r.id != unverified_id]
    store = data["sticky"].setdefault(str(member.guild.id), {})
    if ids:
        store[str(member.id)] = {"roles": ids, "time": time.time()}
    else:
        store.pop(str(member.id), None)
    _prune_sticky(member.guild.id)
    save_data()


async def restore_sticky_roles(member: discord.Member) -> None:
    guild = member.guild
    if member.bot or not sticky_cfg(guild.id)["enabled"]:
        return
    entry = data["sticky"].get(str(guild.id), {}).pop(str(member.id), None)
    if entry is None:
        return
    save_data()
    allow = sticky_cfg(guild.id)["roles"]
    roles = [r for r in sticky_roles_to_restore(guild, entry["roles"], allow) if r not in member.roles]
    if not roles:
        return
    try:
        await member.add_roles(*roles, reason="Sticky roles")
    except discord.HTTPException:
        return
    # They already passed verification before, so don't leave them stuck behind the gate
    cfg = get_config(guild.id)
    unverified = guild.get_role(cfg.get("unverified_role", 0))
    if unverified and unverified in member.roles and any(r.id == cfg.get("verify_role", -1) for r in roles):
        try:
            await member.remove_roles(unverified, reason="Already verified (sticky roles)")
        except discord.HTTPException:
            pass
    await log_action(
        guild, "🔁 Sticky roles restored", f"{member.mention} (`{member.id}`) rejoined.", discord.Color.green(),
        [("Roles", " ".join(r.mention for r in roles))],
    )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    forget_sticky(guild.id, user.id)  # banned people don't get their roles back


stickyroles = app_commands.Group(
    name="stickyroles",
    description="Give members their roles back when they rejoin",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


@stickyroles.command(name="toggle", description="Turn sticky roles on or off")
@app_commands.describe(enabled="On or off")
@manage_guild_only
async def sticky_toggle(interaction: discord.Interaction, enabled: bool):
    sticky_cfg(interaction.guild.id)["enabled"] = enabled
    save_data()
    msg = f"🔁 Sticky roles are now **{'ON' if enabled else 'OFF'}**."
    if enabled:
        msg += "\nFrom now on I remember members' roles when they leave. Keep my role above the roles I should restore."
    await interaction.response.send_message(msg, ephemeral=True)


@stickyroles.command(name="add", description="Only restore specific roles (allow-list)")
@app_commands.describe(role="Role to restore when a member rejoins")
@manage_guild_only
async def sticky_add(interaction: discord.Interaction, role: discord.Role):
    if role.is_default() or role.managed:
        return await interaction.response.send_message("That role can't be restored (it's @everyone or managed).", ephemeral=True)
    roles = sticky_cfg(interaction.guild.id)["roles"]
    if role.id not in roles:
        roles.append(role.id)
        save_data()
    msg = f"**{role.name}** will be restored on rejoin. Only roles on this list are restored now."
    if is_dangerous_role(role):
        msg += "\n⚠️ This role has moderator permissions — it will be restored because you added it explicitly."
    await interaction.response.send_message(msg, ephemeral=True)


@stickyroles.command(name="remove", description="Take a role off the allow-list")
@app_commands.describe(role="Role to remove from the list")
@manage_guild_only
async def sticky_remove(interaction: discord.Interaction, role: discord.Role):
    roles = sticky_cfg(interaction.guild.id)["roles"]
    if role.id in roles:
        roles.remove(role.id)
        save_data()
    note = "" if roles else "\nThe list is empty, so every safe role is restored again."
    await interaction.response.send_message(f"Removed **{role.name}** from the list.{note}", ephemeral=True)


@stickyroles.command(name="list", description="Show the sticky roles settings")
@manage_guild_only
async def sticky_list(interaction: discord.Interaction):
    cfg = sticky_cfg(interaction.guild.id)
    remembered = len(data["sticky"].get(str(interaction.guild.id), {}))
    embed = make_embed("🔁 Sticky roles")
    embed.add_field(name="Status", value=onoff(cfg["enabled"]))
    embed.add_field(name="Members remembered", value=remembered)
    embed.add_field(
        name="Restores",
        value=" ".join(f"<@&{r}>" for r in cfg["roles"]) or "Every role I can assign, except @everyone, managed roles and roles with moderator permissions",
        inline=False,
    )
    embed.set_footer(text="Memories expire after 60 days away, and banned members are forgotten.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@stickyroles.command(name="forget", description="Delete someone's remembered roles")
@app_commands.describe(member="The person to forget")
@manage_guild_only
async def sticky_forget(interaction: discord.Interaction, member: discord.User):
    done = forget_sticky(interaction.guild.id, member.id)
    await interaction.response.send_message(
        f"🧽 Forgot the roles of **{member}**." if done else f"I wasn't remembering any roles for **{member}**.",
        ephemeral=True,
    )


bot.tree.add_command(stickyroles)


# ══════════════════════════════════════════════════════════════
# 🎫 TICKETS
# A panel button opens a private channel; staff can claim it; closing saves a transcript.
# ══════════════════════════════════════════════════════════════
def ticket_cfg(guild_id: int) -> dict:
    return get_config(guild_id).get("ticket", {})


def ticket_of(channel_id: int) -> dict | None:
    return data["tickets"].get(str(channel_id))


def is_ticket_staff(member: discord.Member, cfg: dict) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == cfg.get("staff_role", 0) for r in member.roles)


def open_tickets(guild_id: int, user_id: int) -> list[int]:
    """Channel IDs of the member's open tickets (tickets whose channel vanished are cleaned up)."""
    result = []
    for cid, t in list(data["tickets"].items()):
        if t["guild"] != guild_id or t["user"] != user_id:
            continue
        if bot.get_channel(int(cid)) is None:
            data["tickets"].pop(cid, None)
            save_data()
            continue
        result.append(int(cid))
    return result


async def build_transcript(channel: discord.abc.Messageable) -> str:
    lines = []
    async for m in channel.history(limit=5000, oldest_first=True):
        body = m.content or ""
        for e in m.embeds:
            bits = [e.title, e.description] + [f"{f.name}: {f.value}" for f in e.fields]
            body += " [" + " | ".join(b for b in bits if b) + "]"
        if m.attachments:
            body += " " + " ".join(f"[{a.url}]" for a in m.attachments)
        if body.strip():
            lines.append(f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {m.author.display_name}: {body.strip()}")
    return "\n".join(lines) or "(no messages)"


def make_control_view(claimed: bool = False) -> "TicketControlView":
    """A fresh view per message (never mutate the shared persistent one)."""
    view = TicketControlView()
    for child in view.children:
        if getattr(child, "custom_id", None) == "ticket:claim":
            child.disabled = claimed
            if claimed:
                child.label = "Claimed"
    return view


async def refresh_ticket_message(channel: discord.TextChannel, t: dict) -> None:
    try:
        msg = await channel.fetch_message(t["msg"])
    except (discord.HTTPException, KeyError):
        return
    if not msg.embeds:
        return
    embed = msg.embeds[0].copy()
    claimed_by = f"<@{t['claimed']}>" if t.get("claimed") else "Nobody yet"
    for i, f in enumerate(embed.fields):
        if f.name == "Claimed by":
            embed.set_field_at(i, name="Claimed by", value=claimed_by)
            break
    else:
        embed.add_field(name="Claimed by", value=claimed_by)
    await msg.edit(embed=embed, view=make_control_view(claimed=bool(t.get("claimed"))))


async def get_staff_thread(channel: discord.TextChannel, t: dict) -> discord.Thread | None:
    tid = t.get("thread")
    if not tid:
        return None
    thread = channel.get_thread(tid)
    if thread is None:  # archived threads aren't cached
        try:
            thread = await bot.fetch_channel(tid)
        except discord.HTTPException:
            return None
    return thread if isinstance(thread, discord.Thread) else None


async def create_staff_thread(channel: discord.TextChannel, t: dict, staff_role: discord.Role | None) -> discord.Thread | None:
    """A private thread inside the ticket for the team. The ticket owner can't see it."""
    number = f"{t['number']:04d}"
    try:
        thread = await channel.create_thread(
            name=f"team-notes-{number}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=10080,
            reason=f"Staff thread for ticket #{number}",
        )
    except discord.HTTPException:
        return None  # missing Create Private Threads etc. — the ticket itself still works
    t["thread"] = thread.id
    save_data()
    embed = make_embed(
        "🔒 Staff-only thread",
        "Internal discussion for this ticket. The person who opened it can't see this thread.",
    )
    embed.add_field(name="Ticket", value=f"{channel.mention} — {t.get('subject') or '—'}"[:1000])
    try:  # mentioning the role adds its members to the private thread
        await thread.send(
            staff_role.mention if staff_role else None, embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException:
        pass
    return thread


async def claim_ticket(channel: discord.TextChannel, user: discord.Member) -> str | None:
    """Returns an error message, or None on success."""
    t = ticket_of(channel.id)
    if not t:
        return "This isn't a ticket channel."
    if not is_ticket_staff(user, ticket_cfg(channel.guild.id)):
        return "Only ticket staff can claim tickets."
    if t.get("claimed"):
        return f"This ticket is already claimed by <@{t['claimed']}>."
    t["claimed"] = user.id
    save_data()
    await refresh_ticket_message(channel, t)
    thread = await get_staff_thread(channel, t)
    if thread:
        try:
            await thread.add_user(user)
        except discord.HTTPException:
            pass
    await channel.send(f"🙋 {user.mention} has claimed this ticket.", allowed_mentions=discord.AllowedMentions.none())
    return None


async def close_ticket(channel: discord.TextChannel, closed_by: discord.abc.User, reason: str) -> None:
    t = data["tickets"].pop(str(channel.id), None)  # pop first so it can't be closed twice
    if not t:
        return
    save_data()
    guild = channel.guild
    number = f"{t.get('number', 0):04d}"
    transcript = await build_transcript(channel)
    thread = await get_staff_thread(channel, t)
    notes = await build_transcript(thread) if thread else None  # staff-only: goes to the log, never to the opener
    try:
        opener = await bot.fetch_user(t["user"])
    except discord.HTTPException:
        opener = None

    embed = make_embed(f"🎫 Ticket #{number} closed", color=discord.Color.red())
    embed.add_field(name="Opened by", value=f"<@{t['user']}>")
    embed.add_field(name="Closed by", value=closed_by.mention)
    embed.add_field(name="Claimed by", value=f"<@{t['claimed']}>" if t.get("claimed") else "Nobody")
    embed.add_field(name="Subject", value=(t.get("subject") or "—")[:200], inline=False)
    embed.add_field(name="Reason", value=reason[:500], inline=False)

    log = guild.get_channel(ticket_cfg(guild.id).get("log", 0))
    if isinstance(log, discord.TextChannel):
        try:
            files = [discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{number}.txt")]
            if notes:
                files.append(discord.File(io.BytesIO(notes.encode()), filename=f"ticket-{number}-staff-notes.txt"))
            await log.send(embed=embed, files=files)
        except discord.HTTPException:
            pass
    if opener:
        try:
            await opener.send(
                f"Your ticket in **{guild.name}** was closed. Reason: {reason}\nHere's a copy of the conversation:",
                file=discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{number}.txt"),
            )
        except discord.HTTPException:
            pass
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closed_by}")
    except discord.HTTPException:
        pass


async def create_ticket(guild: discord.Guild, member: discord.Member, subject: str, details: str) -> str:
    cfg = ticket_cfg(guild.id)
    category = guild.get_channel(cfg.get("category", 0))
    if not isinstance(category, discord.CategoryChannel):
        return "Tickets aren't set up properly. Please tell a moderator."
    existing = open_tickets(guild.id, member.id)
    if len(existing) >= cfg.get("max_open", 1):
        return f"You already have an open ticket: <#{existing[0]}>"

    staff_role = guild.get_role(cfg.get("staff_role", 0))
    me_perms = guild.me.guild_permissions

    def grant(**perms) -> discord.PermissionOverwrite:
        # Discord refuses the whole channel if we try to grant a permission we don't have ourselves
        return discord.PermissionOverwrite(**{k: v for k, v in perms.items() if v is not True or getattr(me_perms, k, False)})

    chat = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: grant(**chat, create_public_threads=False, create_private_threads=False),
        guild.me: grant(**chat, manage_channels=True, manage_messages=True, manage_threads=True,
                        create_private_threads=True, send_messages_in_threads=True),
    }
    if staff_role:
        overwrites[staff_role] = grant(**chat, manage_messages=True, send_messages_in_threads=True)

    number = cfg["counter"] = cfg.get("counter", 0) + 1
    try:
        channel = await guild.create_text_channel(
            f"ticket-{number:04d}", category=category, overwrites=overwrites,
            topic=f"Ticket #{number:04d} — opened by {member} ({member.id})", reason=f"Ticket opened by {member}",
        )
    except discord.Forbidden:
        return "I don't have permission to create ticket channels. Please tell a moderator."
    except discord.HTTPException:
        return "I couldn't create the ticket channel (the category might be full). Please tell a moderator."

    t = {"guild": guild.id, "user": member.id, "subject": subject, "number": number,
         "claimed": None, "opened": time.time(), "msg": 0}
    data["tickets"][str(channel.id)] = t
    save_data()

    embed = make_embed(f"🎫 Ticket #{number:04d}", "A staff member will be with you shortly.")
    embed.add_field(name="Opened by", value=member.mention)
    embed.add_field(name="Claimed by", value="Nobody yet")
    embed.add_field(name="Subject", value=subject[:200], inline=False)
    if details.strip():
        embed.add_field(name="Details", value=details[:1000], inline=False)
    ping = f"{member.mention} {staff_role.mention}" if staff_role else member.mention
    msg = await channel.send(
        ping, embed=embed, view=make_control_view(),
        allowed_mentions=discord.AllowedMentions(users=[member], roles=True),
    )
    t["msg"] = msg.id
    save_data()
    if cfg.get("staff_thread", True):
        await create_staff_thread(channel, t, staff_role)
    return f"🎫 Your ticket has been created: {channel.mention}"


class TicketModal(discord.ui.Modal, title="Open a ticket"):
    subject = discord.ui.TextInput(label="What do you need help with?", max_length=100)
    details = discord.ui.TextInput(
        label="Details (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1000,
        placeholder="Tell us more so we can help faster…",
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await create_ticket(interaction.guild, interaction.user, self.subject.value, self.details.value)
        await interaction.followup.send(result, ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Open a ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket:open")
    async def open_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = ticket_cfg(interaction.guild.id)
        if not cfg.get("category"):
            return await interaction.response.send_message(
                "Tickets aren't set up properly. Please tell a moderator.", ephemeral=True
            )
        existing = open_tickets(interaction.guild.id, interaction.user.id)
        if len(existing) >= cfg.get("max_open", 1):
            return await interaction.response.send_message(f"You already have an open ticket: <#{existing[0]}>", ephemeral=True)
        await interaction.response.send_modal(TicketModal())


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="🔒 Closing this ticket in 5 seconds…", view=None)
        await close_ticket(interaction.channel, interaction.user, "Closed with the button")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Okay, the ticket stays open.", view=None)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id="ticket:claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        err = await claim_ticket(interaction.channel, interaction.user)
        await interaction.response.send_message(err or "You claimed this ticket.", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = ticket_of(interaction.channel_id)
        if not t:
            return await interaction.response.send_message("This ticket no longer exists.", ephemeral=True)
        if interaction.user.id != t["user"] and not is_ticket_staff(interaction.user, ticket_cfg(interaction.guild.id)):
            return await interaction.response.send_message(
                "Only the person who opened the ticket, or staff, can close it.", ephemeral=True
            )
        await interaction.response.send_message(
            "Close this ticket? A transcript will be saved.", view=ConfirmCloseView(), ephemeral=True
        )


@bot.tree.command(name="ticket-setup", description="Set up tickets and post the 'Open a ticket' panel")
@app_commands.describe(
    channel="Where to post the panel",
    category="Category where ticket channels are created",
    staff_role="Role that can see and answer tickets",
    log_channel="Where to send transcripts of closed tickets (optional)",
    max_open="Open tickets allowed per member (default 1)",
    staff_thread="Give every ticket a private staff-only thread (default on)",
    title="Panel title",
    description="Panel text",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def ticket_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    category: discord.CategoryChannel,
    staff_role: discord.Role,
    log_channel: discord.TextChannel | None = None,
    max_open: app_commands.Range[int, 1, 10] = 1,
    staff_thread: bool = True,
    title: str = "Support Tickets",
    description: str = "Need help? Click the button below to open a private ticket with our staff.",
):
    if staff_role.is_default():
        return await interaction.response.send_message("Pick a real staff role, not @everyone.", ephemeral=True)
    cfg = get_config(interaction.guild.id)
    cfg["ticket"] = {
        **cfg.get("ticket", {}),
        "category": category.id,
        "staff_role": staff_role.id,
        "log": log_channel.id if log_channel else 0,
        "max_open": max_open,
        "staff_thread": staff_thread,
    }
    save_data()
    await channel.send(embed=make_embed(f"🎫 {title}", description), view=TicketPanelView())
    msg = f"🎫 Tickets are ready — panel posted in {channel.mention}. Staff role: {staff_role.mention}."
    perms = interaction.guild.me.guild_permissions
    needed = [(perms.manage_channels, "Manage Channels"), (perms.manage_roles, "Manage Roles")]
    if staff_thread:
        msg += "\nEvery ticket also gets a private **staff-only thread** (the ticket owner can't see it)."
        needed += [(perms.create_private_threads, "Create Private Threads"), (perms.send_messages_in_threads, "Send Messages in Threads")]
    missing = [n for ok, n in needed if not ok]
    if missing:
        msg += f"\n⚠️ I'm missing **{', '.join(missing)}** — I need it for tickets to work fully."
    if not log_channel:
        msg += "\nTip: add a `log_channel` to keep transcripts of closed tickets."
    await interaction.response.send_message(msg, ephemeral=True)


ticket = app_commands.Group(name="ticket", description="Manage the ticket you're in", guild_only=True)


def _ticket_guard(interaction: discord.Interaction, staff_only: bool) -> tuple[dict | None, str | None]:
    t = ticket_of(interaction.channel_id)
    if not t:
        return None, "This command only works inside a ticket channel."
    if staff_only and not is_ticket_staff(interaction.user, ticket_cfg(interaction.guild.id)):
        return None, "Only ticket staff can do that."
    return t, None


@ticket.command(name="add", description="Add someone to this ticket")
@app_commands.describe(member="Who to add")
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    t, err = _ticket_guard(interaction, staff_only=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction.channel.set_permissions(
        member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
    )
    await interaction.response.send_message(f"➕ Added {member.mention} to this ticket.")


@ticket.command(name="remove", description="Remove someone from this ticket")
@app_commands.describe(member="Who to remove")
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    t, err = _ticket_guard(interaction, staff_only=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if member.id == t["user"]:
        return await interaction.response.send_message("You can't remove the person who opened the ticket.", ephemeral=True)
    await interaction.channel.set_permissions(member, overwrite=None)
    await interaction.response.send_message(f"➖ Removed {member.mention} from this ticket.")


@ticket.command(name="rename", description="Rename this ticket channel")
@app_commands.describe(name="New channel name")
async def ticket_rename(interaction: discord.Interaction, name: str):
    t, err = _ticket_guard(interaction, staff_only=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    clean = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:90]
    if not clean:
        return await interaction.response.send_message("That isn't a usable channel name.", ephemeral=True)
    await interaction.channel.edit(name=clean)
    await interaction.response.send_message(f"✏️ Renamed to **{clean}**.")


@ticket.command(name="notes", description="Open this ticket's staff-only thread (creates it if needed) and add you")
async def ticket_notes(interaction: discord.Interaction):
    t, err = _ticket_guard(interaction, staff_only=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    thread = await get_staff_thread(interaction.channel, t)
    if thread is None:
        staff_role = interaction.guild.get_role(ticket_cfg(interaction.guild.id).get("staff_role", 0))
        thread = await create_staff_thread(interaction.channel, t, staff_role)
        if thread is None:
            return await interaction.followup.send(
                "I couldn't create a thread here — I need the **Create Private Threads** and "
                "**Send Messages in Threads** permissions.", ephemeral=True,
            )
    try:
        await thread.add_user(interaction.user)
    except discord.HTTPException:
        pass
    await interaction.followup.send(f"🔒 Staff thread: {thread.mention}", ephemeral=True)


@ticket.command(name="claim", description="Claim this ticket")
async def ticket_claim(interaction: discord.Interaction):
    err = await claim_ticket(interaction.channel, interaction.user)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction.response.send_message("You claimed this ticket.", ephemeral=True)


@ticket.command(name="close", description="Close this ticket and save a transcript")
@app_commands.describe(reason="Why it's being closed")
async def ticket_close(interaction: discord.Interaction, reason: str = "No reason given"):
    t, err = _ticket_guard(interaction, staff_only=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if interaction.user.id != t["user"] and not is_ticket_staff(interaction.user, ticket_cfg(interaction.guild.id)):
        return await interaction.response.send_message(
            "Only the person who opened the ticket, or staff, can close it.", ephemeral=True
        )
    await interaction.response.send_message("🔒 Closing this ticket in 5 seconds…")
    await close_ticket(interaction.channel, interaction.user, reason)


bot.tree.add_command(ticket)


# ══════════════════════════════════════════════════════════════
# 🍯 HONEYPOT
# A trap channel nobody legitimate should post in. Spam bots and hacked accounts that blast
# messages into every channel get caught automatically.
# ══════════════════════════════════════════════════════════════
HONEYPOT_ACTIONS = {
    # action: (menu label, how the warning describes it)
    "softban": ("Softban — ban then unban, wipes their last 24h of messages",
                "**removed and have their last 24 hours of messages deleted**"),
    "ban": ("Ban — permanent, wipes their last 24h of messages", "**banned**"),
    "kick": ("Kick", "**kicked**"),
    "timeout": ("Timeout for 24 hours", "**timed out for 24 hours**"),
}
_honeypot_busy: set[tuple[int, int]] = set()  # swallow bursts so one spammer is punished once


def honeypot_cfg(guild_id: int) -> dict:
    cfg = get_config(guild_id).setdefault("honeypot", {})
    cfg.setdefault("channels", [])
    cfg.setdefault("action", "softban")
    cfg.setdefault("exempt_roles", [])
    return cfg


def honeypot_embed(action: str) -> discord.Embed:
    return make_embed(
        "🍯 Do not post in this channel!",
        "This channel is a trap for spam bots and hacked accounts.\n"
        f"Anyone who sends a message here is automatically {HONEYPOT_ACTIONS[action][1]}.\n\n"
        "**Don't type anything here.**",
        discord.Color.orange(),
    )


def honeypot_problems(guild: discord.Guild, cfg: dict) -> list[str]:
    perms = guild.me.guild_permissions
    problems = []
    if not perms.manage_messages:
        problems.append("I need **Manage Messages** to delete the trap messages.")
    perm_name, label = {
        "ban": ("ban_members", "Ban Members"), "softban": ("ban_members", "Ban Members"),
        "kick": ("kick_members", "Kick Members"), "timeout": ("moderate_members", "Moderate Members"),
    }[cfg["action"]]
    if not getattr(perms, perm_name):
        problems.append(f"I need **{label}** to {cfg['action']} people.")
    for cid in cfg["channels"]:
        channel = guild.get_channel(cid)
        if channel is None:
            problems.append(f"<#{cid}> no longer exists — remove it with `/honeypot remove`.")
            continue
        everyone = channel.permissions_for(guild.default_role)
        if not (everyone.view_channel and everyone.send_messages):
            problems.append(f"{channel.mention}: @everyone can't see and send messages there, so bots can't fall into it.")
    return problems


def is_honeypot_exempt(member: discord.Member, cfg: dict) -> bool:
    if member.bot or member == member.guild.owner or member.guild_permissions.manage_messages:
        return True
    return any(r.id in cfg.get("exempt_roles", []) for r in member.roles)


async def trigger_honeypot(message: discord.Message, member: discord.Member, cfg: dict) -> None:
    guild, channel = message.guild, message.channel
    action = cfg.get("action", "softban")
    _silent_deletes.add(message.id)  # we log it ourselves
    try:
        await message.delete()
    except discord.HTTPException:
        _silent_deletes.discard(message.id)

    key = (guild.id, member.id)
    if key in _honeypot_busy:
        return
    _honeypot_busy.add(key)
    asyncio.get_running_loop().call_later(30, _honeypot_busy.discard, key)

    reason = f"Honeypot: posted in #{channel.name}"
    if member.top_role >= guild.me.top_role:
        outcome = "⚠️ Message deleted, but I couldn't punish them — their top role is above mine."
    else:
        try:
            await member.send(
                f"You were removed from **{guild.name}** because you posted in a channel that is a trap for spam bots "
                "and hacked accounts. If your account was hacked, change your password and turn on 2FA before rejoining."
            )
        except discord.HTTPException:
            pass
        try:
            if action == "ban":
                await member.ban(reason=reason, delete_message_seconds=86400)
                outcome = "🔨 Banned (last 24h of messages deleted)"
            elif action == "softban":
                await member.ban(reason=reason, delete_message_seconds=86400)
                await guild.unban(member, reason="Honeypot: softban unban")
                outcome = "🧹 Softbanned (last 24h of messages deleted, can rejoin)"
            elif action == "kick":
                await member.kick(reason=reason)
                outcome = "👢 Kicked"
            else:
                await member.timeout(timedelta(hours=24), reason=reason)
                outcome = "🔇 Timed out for 24 hours"
        except discord.HTTPException:
            outcome = "⚠️ Message deleted, but the punishment failed — I'm missing a permission."

    snippet = (message.content or "(no text)").replace("`", "'")[:300]
    fields = [("Action", outcome), ("Account created", discord.utils.format_dt(member.created_at, "R"))]
    if member.joined_at:
        fields.append(("Joined", discord.utils.format_dt(member.joined_at, "R")))
    fields.append(("Message", f"```{snippet}```"))
    await log_action(
        guild, "🍯 Honeypot triggered", f"{member.mention} (`{member.id}`) posted in {channel.mention}",
        discord.Color.orange(), fields,
    )


async def check_honeypot(message: discord.Message) -> bool:
    """True if the message was in a honeypot channel and has been dealt with."""
    cfg = data["config"].get(str(message.guild.id), {}).get("honeypot")
    if not cfg or message.channel.id not in cfg.get("channels", []):
        return False
    member = message.author
    if not isinstance(member, discord.Member) or is_honeypot_exempt(member, cfg):
        return False
    await trigger_honeypot(message, member, cfg)
    return True


honeypot = app_commands.Group(
    name="honeypot",
    description="Catch spam bots and hacked accounts with a trap channel",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)
HONEYPOT_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in HONEYPOT_ACTIONS.items()]


async def _post_honeypot_warning(channel: discord.TextChannel, action: str) -> bool:
    try:
        await channel.send(embed=honeypot_embed(action))
        return True
    except discord.HTTPException:
        return False


@honeypot.command(name="setup", description="Create (or pick) a trap channel and choose what happens to people who post in it")
@app_commands.describe(
    channel="An existing channel to use (leave empty and I'll create #do-not-post)",
    action="What happens to anyone who posts there (default: softban)",
)
@app_commands.choices(action=HONEYPOT_CHOICES)
@manage_guild_only
async def honeypot_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    action: app_commands.Choice[str] | None = None,
):
    guild = interaction.guild
    cfg = honeypot_cfg(guild.id)
    await interaction.response.defer(ephemeral=True)
    created = False
    if channel is None:
        try:
            channel = await guild.create_text_channel(
                "do-not-post", position=0,
                topic="⚠️ Do not post here — anyone who does is removed automatically.",
                overwrites={guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True)},
                reason="Honeypot channel",
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "I couldn't create the channel — give me **Manage Channels**, or pass an existing channel.", ephemeral=True
            )
        created = True
    if channel.id not in cfg["channels"]:
        cfg["channels"].append(channel.id)
    if action:
        cfg["action"] = action.value
    save_data()
    posted = await _post_honeypot_warning(channel, cfg["action"])
    outcome_word = {"softban": "softbanned", "ban": "banned", "kick": "kicked", "timeout": "timed out for 24 hours"}[cfg["action"]]
    lines = [f"🍯 {'Created' if created else 'Using'} {channel.mention} as a honeypot. Anyone who posts there will be **{outcome_word}**."]
    if not posted:
        lines.append("⚠️ I couldn't post the warning message there.")
    problems = honeypot_problems(guild, cfg)
    if problems:
        lines.append("\n".join(f"⚠️ {p}" for p in problems))
    lines.append("Tip: put it near the top of your channel list — spam bots often hit the first channels they see. "
                 "Staff (Manage Messages) and bots are never trapped.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@honeypot.command(name="add", description="Make another existing channel a honeypot")
@app_commands.describe(channel="The channel to add")
@manage_guild_only
async def honeypot_add(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = honeypot_cfg(interaction.guild.id)
    if channel.id in cfg["channels"]:
        return await interaction.response.send_message(f"{channel.mention} is already a honeypot.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg["channels"].append(channel.id)
    save_data()
    await _post_honeypot_warning(channel, cfg["action"])
    await interaction.followup.send(f"🍯 {channel.mention} is now a honeypot.", ephemeral=True)


@honeypot.command(name="remove", description="Stop using a channel as a honeypot")
@app_commands.describe(channel="The channel to remove")
@manage_guild_only
async def honeypot_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = honeypot_cfg(interaction.guild.id)
    if channel.id not in cfg["channels"]:
        return await interaction.response.send_message(f"{channel.mention} isn't a honeypot.", ephemeral=True)
    cfg["channels"].remove(channel.id)
    save_data()
    left = "" if cfg["channels"] else "\nNo honeypot channels are left, so the honeypot is off."
    await interaction.response.send_message(
        f"Removed {channel.mention} from the honeypots. People can post there safely now.{left}", ephemeral=True
    )


@honeypot.command(name="action", description="Change what happens to people who post in a honeypot")
@app_commands.describe(action="What to do to them")
@app_commands.choices(action=HONEYPOT_CHOICES)
@manage_guild_only
async def honeypot_action(interaction: discord.Interaction, action: app_commands.Choice[str]):
    cfg = honeypot_cfg(interaction.guild.id)
    cfg["action"] = action.value
    save_data()
    msg = f"Honeypot action is now **{action.value}**. The warning messages in the trap channels still show the old action — run `/honeypot setup` with the channel again to post a new one."
    problems = honeypot_problems(interaction.guild, cfg)
    if problems:
        msg += "\n" + "\n".join(f"⚠️ {p}" for p in problems)
    await interaction.response.send_message(msg, ephemeral=True)


@honeypot.command(name="exempt", description="Toggle a role that is never trapped (staff with Manage Messages already aren't)")
@app_commands.describe(role="Role to exempt (run again to remove)")
@manage_guild_only
async def honeypot_exempt(interaction: discord.Interaction, role: discord.Role):
    cfg = honeypot_cfg(interaction.guild.id)
    if role.id in cfg["exempt_roles"]:
        cfg["exempt_roles"].remove(role.id)
        msg = f"**{role.name}** can be trapped again."
    else:
        cfg["exempt_roles"].append(role.id)
        msg = f"**{role.name}** is now exempt from the honeypot."
    save_data()
    await interaction.response.send_message(msg, ephemeral=True)


@honeypot.command(name="status", description="Show the honeypot settings and check that everything will work")
@manage_guild_only
async def honeypot_status(interaction: discord.Interaction):
    cfg = honeypot_cfg(interaction.guild.id)
    embed = make_embed("🍯 Honeypot", color=discord.Color.orange())
    embed.add_field(name="Channels", value=" ".join(f"<#{c}>" for c in cfg["channels"]) or "none — run `/honeypot setup`", inline=False)
    embed.add_field(name="Action", value=cfg["action"])
    embed.add_field(name="Exempt roles", value=" ".join(f"<@&{r}>" for r in cfg["exempt_roles"]) or "none")
    problems = honeypot_problems(interaction.guild, cfg) if cfg["channels"] else []
    embed.add_field(name="Check", value="\n".join(f"⚠️ {p}" for p in problems) or ("✅ Looks good" if cfg["channels"] else "—"), inline=False)
    embed.set_footer(text="Staff with Manage Messages, the server owner and bots are never trapped.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(honeypot)


# ══════════════════════════════════════════════════════════════
# 🛍️ SHOP
# Spend coins (from /daily, /work, /gamble, trivia…) on roles and items that admins add.
#   • Role items give a role the moment they're bought.
#   • Normal items go in your inventory; /redeem uses one and pings staff (via the mod log) to hand over the reward.
# ══════════════════════════════════════════════════════════════
SHOP_PAGE_SIZE = 5


def shop_data(guild_id: int) -> dict:
    shop = data["shop"].setdefault(str(guild_id), {})
    shop.setdefault("next_id", 1)
    shop.setdefault("items", {})
    return shop


def active_items(guild_id: int) -> dict[str, dict]:
    return {iid: it for iid, it in shop_data(guild_id)["items"].items() if it.get("active", True)}


def inventory_of(guild_id: int, user_id: int) -> dict[str, int]:
    return data["inventory"].setdefault(str(guild_id), {}).setdefault(str(user_id), {})


def find_item(
    guild_id: int, query: str, include_inactive: bool = False, only_ids: set[str] | None = None, exact: bool = False
) -> tuple[str, dict] | None:
    """Find one item by #number, exact name, or (unless exact=True) an unambiguous start of its name."""
    items = {
        iid: it for iid, it in shop_data(guild_id)["items"].items()
        if (include_inactive or it.get("active", True)) and (only_ids is None or iid in only_ids)
    }
    q = query.strip().lower().lstrip("#")
    if not q:
        return None
    if q.isdigit() and q in items:
        return q, items[q]
    for iid, it in items.items():
        if it["name"].lower() == q:
            return iid, it
    if exact:  # admin actions must never guess ("cake" must not hit "Cupcake")
        return None
    def starts(name: str) -> bool:  # start of the name or of any word in it ("cake" -> "Birthday Cake", not "Cupcake")
        name = name.lower()
        return name.startswith(q) or any(w.startswith(q) for w in name.split())
    matches = [(iid, it) for iid, it in items.items() if starts(it["name"])]
    return matches[0] if len(matches) == 1 else None


def role_item_problem(guild: discord.Guild, role: discord.Role | None) -> str | None:
    if role is None:
        return "The role for this item no longer exists."
    if role.is_default() or role.managed or is_dangerous_role(role):
        return "That role can't be sold (it's @everyone, managed, or has moderator permissions)."
    if role >= guild.me.top_role:
        return "I can't hand out that role — it's above my highest role."
    return None


def stock_text(item: dict) -> str:
    stock = item.get("stock", -1)
    return "unlimited" if stock < 0 else ("SOLD OUT" if stock == 0 else f"{stock} left")


def shop_pages(guild: discord.Guild, balance: int) -> list[discord.Embed]:
    items = sorted(active_items(guild.id).items(), key=lambda kv: int(kv[0]))
    if not items:
        return [make_embed("🛍️ Shop", "The shop is empty right now. Admins can add items with `/shopadmin add`.")]
    chunks = [items[i:i + SHOP_PAGE_SIZE] for i in range(0, len(items), SHOP_PAGE_SIZE)]
    pages = []
    for n, chunk in enumerate(chunks, 1):
        embed = make_embed("🛍️ Shop", f"Your balance: **{balance:,}** coins · buy with `/buy <item>`")
        for iid, it in chunk:
            role = guild.get_role(it.get("role", 0)) if it.get("role") else None
            if it.get("role"):
                kind = f"Gives the {role.mention} role" if role else "Role reward (role missing)"
            else:
                kind = "Item — use it with `/redeem`"
            value = (f"{it['description']}\n" if it.get("description") else "") + f"{kind} · {stock_text(it)}"
            embed.add_field(name=f"#{iid} · {it['name']} — {it['price']:,} 🪙", value=value[:1024], inline=False)
        embed.set_footer(text=f"Page {n}/{len(chunks)}")
        pages.append(embed)
    return pages


class ShopView(discord.ui.View):
    def __init__(self, user_id: int, pages: list[discord.Embed]):
        super().__init__(timeout=120)
        self.user_id, self.pages, self.page = user_id, pages, 0
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Run `/shop` yourself to browse.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def shop_item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    return [
        app_commands.Choice(name=f"{it['name']} — {it['price']:,} coins"[:100], value=it["name"])
        for it in active_items(interaction.guild.id).values() if cur in it["name"].lower()
    ][:25]


async def owned_item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    inv = inventory_of(interaction.guild.id, interaction.user.id)
    items = shop_data(interaction.guild.id)["items"]
    out = []
    for iid, qty in inv.items():
        name = items.get(iid, {}).get("name")
        if qty > 0 and name and cur in name.lower():
            out.append(app_commands.Choice(name=f"{name} (×{qty})"[:100], value=name))
    return out[:25]


@bot.tree.command(name="shop", description="Browse the shop")
@app_commands.guild_only()
async def shop_cmd(interaction: discord.Interaction):
    pages = shop_pages(interaction.guild, account(interaction.user.id)["bal"])
    if len(pages) == 1:
        return await interaction.response.send_message(embed=pages[0])
    view = ShopView(interaction.user.id, pages)
    await interaction.response.send_message(embed=pages[0], view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="buy", description="Buy something from the shop")
@app_commands.describe(item="Item name or its #number from /shop", quantity="How many (roles can only be bought once)")
@app_commands.guild_only()
async def buy(interaction: discord.Interaction, item: str, quantity: app_commands.Range[int, 1, 100] = 1):
    guild, member = interaction.guild, interaction.user
    found = find_item(guild.id, item)
    if not found:
        return await interaction.response.send_message(
            f"I couldn't find a single item matching `{item}`. Use the full name or its #number from `/shop`.", ephemeral=True
        )
    iid, it = found
    role = guild.get_role(it["role"]) if it.get("role") else None
    if it.get("role"):
        if problem := role_item_problem(guild, role):
            return await interaction.response.send_message(problem, ephemeral=True)
        if quantity > 1:
            return await interaction.response.send_message("A role can only be bought once.", ephemeral=True)
        if role in member.roles:
            return await interaction.response.send_message(f"You already have **{role.name}**.", ephemeral=True)
    stock = it.get("stock", -1)
    if stock == 0:
        return await interaction.response.send_message(f"**{it['name']}** is sold out.", ephemeral=True)
    if 0 <= stock < quantity:
        return await interaction.response.send_message(f"Only **{stock}** of **{it['name']}** left.", ephemeral=True)
    total = it["price"] * quantity
    acct = account(member.id)
    if acct["bal"] < total:
        return await interaction.response.send_message(
            f"You need **{total:,}** coins but only have **{acct['bal']:,}**.", ephemeral=True
        )

    acct["bal"] -= total  # take payment first (nothing awaits between the checks above and here)
    if stock >= 0:
        it["stock"] = stock - quantity
    it["sold"] = it.get("sold", 0) + quantity
    if role is not None:
        try:
            await member.add_roles(role, reason=f"Bought from the shop (#{iid})")
        except discord.HTTPException:
            acct["bal"] += total  # couldn't deliver → full refund
            it["stock"] = stock
            it["sold"] -= quantity
            save_data()
            return await interaction.response.send_message(
                "I couldn't give you that role, so you haven't been charged. Please tell a moderator.", ephemeral=True
            )
        what = f"the **{role.name}** role"
    else:
        inv = inventory_of(guild.id, member.id)
        inv[iid] = inv.get(iid, 0) + quantity
        what = f"**{it['name']}**" + (f" ×{quantity}" if quantity > 1 else "")
    save_data()
    tail = "" if role is not None else " Use it with `/redeem`."
    await interaction.response.send_message(
        f"🛍️ {member.mention} bought {what} for **{total:,}** coins. Balance: **{acct['bal']:,}**.{tail}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


buy.autocomplete("item")(shop_item_autocomplete)


@bot.tree.command(name="inventory", description="See the items you own")
@app_commands.describe(member="Whose inventory (defaults to you)")
@app_commands.guild_only()
async def inventory(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    inv = data["inventory"].get(str(interaction.guild.id), {}).get(str(member.id), {})
    items = shop_data(interaction.guild.id)["items"]
    lines = [
        f"**{items.get(iid, {}).get('name', f'Item #{iid}')}** ×{qty}"
        for iid, qty in sorted(inv.items(), key=lambda kv: int(kv[0])) if qty > 0
    ]
    embed = make_embed(f"🎒 {member.display_name}'s inventory", "\n".join(lines) or "Nothing yet — see `/shop`!")
    embed.set_footer(text=f"{account(member.id)['bal']:,} coins")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="redeem", description="Use an item you own — staff are notified to give you the reward")
@app_commands.describe(item="Which item", quantity="How many to use")
@app_commands.guild_only()
async def redeem(interaction: discord.Interaction, item: str, quantity: app_commands.Range[int, 1, 100] = 1):
    guild, member = interaction.guild, interaction.user
    inv = inventory_of(guild.id, member.id)
    found = find_item(guild.id, item, include_inactive=True, only_ids={iid for iid, q in inv.items() if q > 0})
    if not found:
        return await interaction.response.send_message(
            f"You don't own anything matching `{item}`. Check `/inventory`.", ephemeral=True
        )
    iid, it = found
    if inv[iid] < quantity:
        return await interaction.response.send_message(f"You only have **{inv[iid]}** of **{it['name']}**.", ephemeral=True)
    log_channel = guild.get_channel(get_config(guild.id).get("modlog", 0))
    if not isinstance(log_channel, discord.TextChannel):
        return await interaction.response.send_message(
            "Redeeming isn't set up yet — ask an admin to set a mod log with `/modlog`, so staff can see your request. "
            "You haven't lost your item.", ephemeral=True,
        )
    inv[iid] -= quantity
    if inv[iid] <= 0:
        del inv[iid]
    save_data()
    await log_action(
        guild, "🛍️ Item redeemed", f"{member.mention} used **{it['name']}** ×{quantity}.", discord.Color.gold(),
        [("To do", "Give them their reward, then you're done."), ("About the item", it.get("description") or "—")],
    )
    await interaction.response.send_message(
        f"✅ You used **{it['name']}** ×{quantity}. Staff have been notified and will sort out your reward.",
    )


redeem.autocomplete("item")(owned_item_autocomplete)


shopadmin = app_commands.Group(
    name="shopadmin",
    description="Manage the shop",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


@shopadmin.command(name="add", description="Add an item to the shop")
@app_commands.describe(
    name="Item name (up to 40 characters)",
    price="Price in coins",
    role="Role given when bought (leave empty for a normal item)",
    stock="How many can be sold (-1 = unlimited)",
    description="Short description shown in the shop",
)
@manage_guild_only
async def shopadmin_add(
    interaction: discord.Interaction,
    name: str,
    price: app_commands.Range[int, 1, 10_000_000],
    role: discord.Role | None = None,
    stock: app_commands.Range[int, -1, 100_000] = -1,
    description: str = "",
):
    guild = interaction.guild
    name = name.strip()
    if not name or len(name) > 40:
        return await interaction.response.send_message("Item names must be 1–40 characters.", ephemeral=True)
    if name.lstrip("#").isdigit():
        return await interaction.response.send_message(
            "Names can't be just a number (numbers are used to pick items from `/shop`).", ephemeral=True
        )
    if any(it["name"].lower() == name.lower() for it in active_items(guild.id).values()):
        return await interaction.response.send_message(f"There's already an item called **{name}**.", ephemeral=True)
    if role is not None:
        problem = role_error(interaction, role) or role_item_problem(guild, role)
        if problem:
            return await interaction.response.send_message(f"**{role.name}**: {problem}", ephemeral=True)
    shop = shop_data(guild.id)
    iid = str(shop["next_id"])
    shop["next_id"] += 1
    shop["items"][iid] = {
        "name": name, "price": price, "description": description.strip()[:200], "role": role.id if role else 0,
        "stock": stock, "sold": 0, "active": True,
    }
    save_data()
    extra = f" — gives the **{role.name}** role" if role else ""
    await interaction.response.send_message(
        f"✅ Added **{name}** (#{iid}) for **{price:,}** coins ({'unlimited' if stock < 0 else stock} in stock){extra}.",
        ephemeral=True,
    )


@shopadmin.command(name="remove", description="Take an item off sale (people who own it keep it)")
@app_commands.describe(item="Item name or #number")
@manage_guild_only
async def shopadmin_remove(interaction: discord.Interaction, item: str):
    found = find_item(interaction.guild.id, item, exact=True)
    if not found:
        return await interaction.response.send_message(
            f"I couldn't find an item called `{item}`. Use its exact name or its #number from `/shop`.", ephemeral=True
        )
    iid, it = found
    it["active"] = False
    save_data()
    await interaction.response.send_message(
        f"🗑️ **{it['name']}** is no longer for sale. Anyone who already owns it can still use it.", ephemeral=True
    )


@shopadmin.command(name="edit", description="Change an item's name, price, description or stock")
@app_commands.describe(
    item="Item name or #number", name="New name", price="New price", description="New description",
    stock="New stock (-1 = unlimited, 0 = sold out)",
)
@manage_guild_only
async def shopadmin_edit(
    interaction: discord.Interaction,
    item: str,
    name: str | None = None,
    price: app_commands.Range[int, 1, 10_000_000] | None = None,
    stock: app_commands.Range[int, -1, 100_000] | None = None,
    description: str | None = None,
):
    found = find_item(interaction.guild.id, item, exact=True)
    if not found:
        return await interaction.response.send_message(
            f"I couldn't find an item called `{item}`. Use its exact name or its #number from `/shop`.", ephemeral=True
        )
    iid, it = found
    if name is not None:
        name = name.strip()
        if not name or len(name) > 40 or name.lstrip("#").isdigit():
            return await interaction.response.send_message("Names must be 1–40 characters and not just a number.", ephemeral=True)
        if any(o["name"].lower() == name.lower() for k, o in active_items(interaction.guild.id).items() if k != iid):
            return await interaction.response.send_message(f"There's already an item called **{name}**.", ephemeral=True)
        it["name"] = name
    if price is not None:
        it["price"] = price
    if stock is not None:
        it["stock"] = stock
    if description is not None:
        it["description"] = description.strip()[:200]
    save_data()
    await interaction.response.send_message(
        f"✏️ Updated **{it['name']}** (#{iid}): {it['price']:,} coins, {stock_text(it)}.", ephemeral=True
    )


@shopadmin.command(name="give", description="Give someone an item for free")
@app_commands.describe(member="Who gets it", item="Item name or #number", quantity="How many")
@manage_guild_only
async def shopadmin_give(
    interaction: discord.Interaction, member: discord.Member, item: str, quantity: app_commands.Range[int, 1, 100] = 1
):
    guild = interaction.guild
    found = find_item(guild.id, item, exact=True)
    if not found:
        return await interaction.response.send_message(
            f"I couldn't find an item called `{item}`. Use its exact name or its #number from `/shop`.", ephemeral=True
        )
    iid, it = found
    if it.get("role"):
        role = guild.get_role(it["role"])
        if problem := role_item_problem(guild, role):
            return await interaction.response.send_message(problem, ephemeral=True)
        await member.add_roles(role, reason=f"Given by {interaction.user} (shop item #{iid})")
        what = f"the **{role.name}** role"
    else:
        inv = inventory_of(guild.id, member.id)
        inv[iid] = inv.get(iid, 0) + quantity
        save_data()
        what = f"**{it['name']}**" + (f" ×{quantity}" if quantity > 1 else "")
    await interaction.response.send_message(f"🎁 Gave {member.mention} {what}.", allowed_mentions=discord.AllowedMentions.none())


shopadmin_remove.autocomplete("item")(shop_item_autocomplete)
shopadmin_edit.autocomplete("item")(shop_item_autocomplete)
shopadmin_give.autocomplete("item")(shop_item_autocomplete)
bot.tree.add_command(shopadmin)


# ══════════════════════════════════════════════════════════════
# 🎁 COIN GIFTS & ADMIN COINS
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="coinsgift", description="Send someone a gift of coins, with an optional message")
@app_commands.describe(
    member="Who gets the gift",
    amount="How many coins to give (they come out of your balance)",
    message="A note to go with it",
    anonymous="Hide that it's from you (staff can still see it in the mod log)",
)
@app_commands.guild_only()
async def coinsgift(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000],
    anonymous: bool = False,
    message: str = "",
):
    sender = interaction.user
    if member.bot or member.id == sender.id:
        return await interaction.response.send_message("You can't send a gift to that user.", ephemeral=True)
    mine = account(sender.id)
    if mine["bal"] < amount:
        return await interaction.response.send_message(
            f"You only have **{mine['bal']:,}** coins — you can't gift **{amount:,}**.", ephemeral=True
        )
    mine["bal"] -= amount
    account(member.id)["bal"] += amount
    save_data()

    who = "Someone" if anonymous else sender.mention
    embed = make_embed("🎁 A gift!", f"{who} sent {member.mention} **{amount:,}** coins!", discord.Color.gold())
    if message.strip():
        embed.add_field(name="Message", value=message.strip()[:200], inline=False)
    ping = discord.AllowedMentions(everyone=False, roles=False, users=[member])
    if not anonymous:
        return await interaction.response.send_message(content=member.mention, embed=embed, allowed_mentions=ping)

    # Anonymous: only the sender gets a (private) reply, and the gift is posted as a plain channel message
    await interaction.response.send_message(
        f"🎁 Your anonymous gift of **{amount:,}** coins to {member.mention} was sent. "
        f"You have **{mine['bal']:,}** left.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )
    try:
        await interaction.channel.send(content=member.mention, embed=embed, allowed_mentions=ping)
    except discord.HTTPException:
        await interaction.followup.send("The coins were sent, but I couldn't post the gift message in this channel.", ephemeral=True)
    await log_action(
        interaction.guild, "🎁 Anonymous coin gift", f"{sender.mention} → {member.mention}: **{amount:,}** coins",
        discord.Color.gold(), [("Message", message.strip()[:200] or "—")],
    )


admincoins = app_commands.Group(
    name="admincoins",
    description="Add, remove or set a member's coins",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


async def _admin_coin_change(
    interaction: discord.Interaction, member: discord.Member, title: str, summary: str, reason: str, before: int, after: int,
    notify: str | None,
) -> None:
    """Shared tail of the admin coin commands: log it, tell the member, answer the admin."""
    save_data()
    await log_action(
        interaction.guild, title, f"{member.mention} — by {interaction.user.mention}", discord.Color.gold(),
        [("Change", summary), ("Balance", f"{before:,} → {after:,}"), ("Reason", reason.strip()[:300] or "—")],
    )
    if notify:
        try:
            await member.send(f"{notify} (in **{interaction.guild.name}**){' — ' + reason.strip()[:300] if reason.strip() else ''}")
        except discord.HTTPException:
            pass
    await interaction.response.send_message(
        f"💰 {summary} {member.mention}'s balance: **{before:,}** → **{after:,}** coins.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@admincoins.command(name="add", description="Give a member coins out of thin air")
@app_commands.describe(member="Who gets the coins", amount="How many", reason="Why (shown to them and in the mod log)")
@manage_guild_only
async def admincoins_add(
    interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000], reason: str = ""
):
    if member.bot:
        return await interaction.response.send_message("Bots don't have coins.", ephemeral=True)
    acct = account(member.id)
    before = acct["bal"]
    acct["bal"] += amount
    await _admin_coin_change(
        interaction, member, "💰 Coins added", f"Added **{amount:,}** coins.", reason, before, acct["bal"],
        notify=f"💰 You received **{amount:,}** coins from the staff",
    )


@admincoins.command(name="remove", description="Take coins away from a member (it stops at 0)")
@app_commands.describe(member="Whose coins", amount="How many to take", reason="Why (shown in the mod log)")
@manage_guild_only
async def admincoins_remove(
    interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000_000], reason: str = ""
):
    if member.bot:
        return await interaction.response.send_message("Bots don't have coins.", ephemeral=True)
    acct = account(member.id)
    before = acct["bal"]
    taken = min(amount, before)
    acct["bal"] = before - taken
    note = f"Removed **{taken:,}** coins" + (f" (they only had {before:,})" if taken < amount else "") + "."
    await _admin_coin_change(interaction, member, "💰 Coins removed", note, reason, before, acct["bal"], notify=None)


@admincoins.command(name="set", description="Set a member's balance to an exact amount")
@app_commands.describe(member="Whose coins", amount="The new balance", reason="Why (shown in the mod log)")
@manage_guild_only
async def admincoins_set(
    interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 0, 1_000_000_000], reason: str = ""
):
    if member.bot:
        return await interaction.response.send_message("Bots don't have coins.", ephemeral=True)
    acct = account(member.id)
    before = acct["bal"]
    acct["bal"] = amount
    await _admin_coin_change(
        interaction, member, "💰 Coins set", f"Set to **{amount:,}** coins.", reason, before, amount, notify=None
    )


bot.tree.add_command(admincoins)


# ══════════════════════════════════════════════════════════════
# 🛠️ ADMIN PANEL
# /adminpanel opens a private (only you can see it) control panel: status of every system,
# plus buttons and menus for the settings you change most. Changes are logged to the mod log.
# ══════════════════════════════════════════════════════════════
SLASH_ONLY = {"adminpanel", "admin"}  # commands that can't work as a plain text message (the prefix says so)
RULE_KEYS = ("spam", "invites", "links", "badwords", "mentions", "caps")
MIN_AGE_STEPS = [0, 1, 3, 7, 14, 30]
MAX_OPEN_STEPS = [1, 2, 3, 5]
PANEL_PAGES = [
    # key, emoji, label, description
    ("overview", "📊", "Overview", "Status of everything, and what needs attention"),
    ("automod", "🤖", "Auto-moderation", "Switch rules on and off"),
    ("protection", "🛡️", "Protection", "Raid protection, raid mode and the honeypot"),
    ("members", "👥", "Members & roles", "Sticky roles and the autorole"),
    ("logging", "📜", "Logging", "Choose the mod log channel"),
    ("support", "🎫", "Tickets & modmail", "Ticket options and modmail status"),
]


def _latency_text() -> str:
    latency = bot.latency
    return f"{round(latency * 1000)}ms" if math.isfinite(latency) else "—"


class AdminPanel(discord.ui.View):
    def __init__(self, user: discord.Member, guild: discord.Guild, page: str = "overview"):
        super().__init__(timeout=600)
        self.user, self.guild, self.page = user, guild, page
        self.origin: discord.Interaction | None = None
        self._build()

    # ───────────── plumbing ─────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your panel — run `/adminpanel` yourself.", ephemeral=True)
            return False
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        if not member.guild_permissions.administrator:
            await interaction.response.send_message("You no longer have the Administrator permission.", ephemeral=True)
            self.stop()
            return False
        return True

    async def on_timeout(self) -> None:
        if self.origin:
            try:
                await self.origin.edit_original_response(content="⌛ This panel expired — run `/adminpanel` again.", view=None)
            except discord.HTTPException:
                pass

    def _build(self) -> None:
        self.clear_items()
        menu = discord.ui.Select(
            placeholder="Choose a section…", row=0,
            options=[discord.SelectOption(label=label, value=key, emoji=emoji, description=desc, default=key == self.page)
                     for key, emoji, label, desc in PANEL_PAGES],
        )

        async def pick(interaction: discord.Interaction):
            self.page = menu.values[0]
            await self._redraw(interaction)

        menu.callback = pick
        self.add_item(menu)
        getattr(self, f"_controls_{self.page}")()

        async def close(interaction: discord.Interaction):
            self.stop()
            await interaction.response.edit_message(content="Panel closed.", embed=None, view=None)

        self._button("Refresh", discord.ButtonStyle.secondary, 4, self._redraw, emoji="🔄")
        self._button("Close", discord.ButtonStyle.secondary, 4, close, emoji="✖️")

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        return clamp_fields(getattr(self, f"_embed_{self.page}")())

    def _button(self, label, style, row, callback, *, emoji=None, disabled=False) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, row=row, emoji=emoji, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    async def _audit(self, interaction: discord.Interaction, text: str) -> None:
        await log_action(self.guild, "🛠️ Admin panel", f"{interaction.user.mention}: {text}", discord.Color.blurple())

    def _toggle(self, label, row, get, put, what, disabled=False) -> None:
        on = get()

        async def click(interaction: discord.Interaction):
            new = not get()
            put(new)
            save_data()
            await self._audit(interaction, f"{what} turned **{'on' if new else 'off'}**")
            await self._redraw(interaction)

        self._button(f"{label}: {'ON' if on else 'OFF'}", discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                     row, click, disabled=disabled)

    def _cycle(self, label, row, steps, get, put, fmt, what, disabled=False) -> None:
        async def click(interaction: discord.Interaction):
            current = get()
            new = steps[(steps.index(current) + 1) % len(steps)] if current in steps else steps[0]
            put(new)
            save_data()
            await self._audit(interaction, f"{what} set to **{fmt(new)}**")
            await self._redraw(interaction)

        self._button(f"{label}: {fmt(get())}", discord.ButtonStyle.primary, row, click, disabled=disabled)

    # ───────────── 📊 overview ─────────────
    def _attention(self) -> list[str]:
        g = self.guild
        cfg = get_config(g.id)
        found = []
        if not isinstance(g.get_channel(cfg.get("modlog", 0)), discord.TextChannel):
            found.append("No mod log channel — set one in **Logging**.")
        perms = g.me.guild_permissions
        missing = [label for attr, label in BOT_PERMS_NEEDED.items() if not getattr(perms, attr)]
        if missing:
            found.append("I'm missing permissions: " + ", ".join(missing) + ".")
        am = automod_cfg(g.id)
        if am["enabled"] and not any(am[r]["on"] for r in RULE_KEYS):
            found.append("Auto-mod is on but no rules are enabled.")
        hp = honeypot_cfg(g.id)
        if hp["channels"]:
            found.extend(honeypot_problems(g, hp))
        return found

    def _embed_overview(self) -> discord.Embed:
        g = self.guild
        gid = g.id
        cfg = get_config(gid)
        am = automod_cfg(gid)
        e = make_embed(f"🛠️ {g.name} — admin panel",
                       "Pick a section from the menu. Changes apply instantly and are logged to your mod log.")
        if g.icon:
            e.set_thumbnail(url=g.icon.url)

        problems = self._attention()
        e.add_field(name="⚠️ Needs attention" if problems else "✅ All good",
                    value="\n".join(f"• {p}" for p in problems) or "Nothing needs your attention right now.", inline=False)

        active = sum(1 for r in RULE_KEYS if am[r]["on"])
        e.add_field(name="🤖 Auto-mod", value=f"{onoff(am['enabled'])} · {active}/{len(RULE_KEYS)} rules\nStrikes {onoff(am['strikes'])}")
        modlog = g.get_channel(cfg.get("modlog", 0))
        e.add_field(name="📜 Mod log", value=modlog.mention if modlog else "❌ not set")
        raid = cfg.get("antiraid", {})
        raid_bits = [f"detection {onoff(raid.get('detect', False))}"]
        if raid.get("min_age_days"):
            raid_bits.append(f"min age {raid['min_age_days']}d")
        if raid.get("raid_until", 0) > time.time():
            raid_bits.append("🔴 **raid mode ON**")
        e.add_field(name="🚨 Anti-raid", value="\n".join(raid_bits))
        hp = honeypot_cfg(gid)
        e.add_field(name="🍯 Honeypot", value=f"{len(hp['channels'])} channel(s) · {hp['action']}" if hp["channels"] else "❌ none")
        verify = g.get_role(cfg.get("verify_role", 0))
        e.add_field(name="✅ Verification", value=verify.mention if verify else "❌ not set up")
        auto = g.get_role(cfg.get("autorole", 0))
        e.add_field(name="🎭 Autorole", value=auto.mention if auto else "❌ none")
        st = sticky_cfg(gid)
        e.add_field(name="🔁 Sticky roles", value=f"{onoff(st['enabled'])} · {len(data['sticky'].get(str(gid), {}))} remembered")
        modmail_on = isinstance(g.get_channel(cfg.get("modmail_category", 0)), discord.CategoryChannel)
        modmail_open = sum(1 for t in data["modmail"].values() if t["guild"] == gid)
        e.add_field(name="📬 Modmail", value=f"{onoff(modmail_on)} · {modmail_open} open")
        tickets_open = sum(1 for t in data["tickets"].values() if t["guild"] == gid)
        e.add_field(name="🎫 Tickets", value=f"{onoff(bool(ticket_cfg(gid).get('category')))} · {tickets_open} open")
        sold = sum(i.get("sold", 0) for i in shop_data(gid)["items"].values())
        e.add_field(name="🛍️ Shop", value=f"{len(active_items(gid))} item(s) · {sold} sold")
        warns = sum(len(v) for v in data["warnings"].get(str(gid), {}).values())
        e.add_field(name="⚠️ Warnings", value=f"{warns} issued")
        coins = sum(a.get("bal", 0) for a in data["economy"].values())
        e.add_field(name="💰 Coins", value=f"{coins:,} in circulation\n(shared across servers)")
        uptime = timedelta(seconds=int(time.time() - START_TIME))
        e.add_field(name="🤖 Bot", value=f"{_latency_text()} · up {uptime}\n{len(bot.guilds)} server(s) · {g.member_count} members here")
        return e

    def _controls_overview(self) -> None:
        pass  # read-only; use the menu to jump to a section

    # ───────────── 🤖 auto-moderation ─────────────
    def _embed_automod(self) -> discord.Embed:
        e = automod_embed(self.guild.id)
        e.set_footer(text="Thresholds, bad words, allowed links and exemptions: /automod · one-step preset: /automod-setup")
        return e

    def _controls_automod(self) -> None:
        gid = self.guild.id

        def getter(key, sub=None):
            return lambda: automod_cfg(gid)[key][sub] if sub else automod_cfg(gid)[key]

        def setter(key, sub=None):
            def put(value):
                c = automod_cfg(gid)
                if sub:
                    c[key][sub] = value
                else:
                    c[key] = value
            return put

        self._toggle("Auto-mod", 1, getter("enabled"), setter("enabled"), "Auto-moderation")
        self._toggle("Strikes", 1, getter("strikes"), setter("strikes"), "Automod strikes")
        names = {"spam": "Spam", "invites": "Invites", "links": "Links", "badwords": "Bad words", "mentions": "Mentions", "caps": "Caps"}
        for i, key in enumerate(RULE_KEYS):
            self._toggle(names[key], 1 if i < 3 else 2, getter(key, "on"), setter(key, "on"), f"The {names[key].lower()} rule")

    # ───────────── 🛡️ protection ─────────────
    def _embed_protection(self) -> discord.Embed:
        g = self.guild
        cfg = get_config(g.id)
        raid = cfg.get("antiraid", {})
        e = make_embed("🛡️ Protection", "Join protection and the honeypot trap.")
        age = raid.get("min_age_days", 0)
        e.add_field(name="Minimum account age", value=f"{age} day(s)" if age else "off")
        e.add_field(name="Raid detection", value=f"{onoff(raid.get('detect', False))} {raid.get('joins', 5)} joins / {raid.get('seconds', 10)}s")
        left = int(raid.get("raid_until", 0) - time.time())
        e.add_field(name="Raid mode", value=f"🔴 active ({math.ceil(left / 60)}m left)" if left > 0 else "off")
        hp = honeypot_cfg(g.id)
        e.add_field(name="🍯 Honeypot", value=" ".join(f"<#{c}>" for c in hp["channels"]) or "none — run `/honeypot setup`")
        e.add_field(name="Honeypot action", value=hp["action"])
        verify = g.get_role(cfg.get("verify_role", 0))
        e.add_field(name="✅ Verification", value=verify.mention if verify else "not set up — `/verification-setup`")
        e.set_footer(text="Raid mode kicks every new join for 10 minutes. Fine-tune with /antiraid and /honeypot.")
        return e

    def _controls_protection(self) -> None:
        gid = self.guild.id

        def raid() -> dict:
            return get_config(gid).setdefault("antiraid", {})

        def put_detect(value):
            raid()["detect"] = value

        self._toggle("Raid detection", 1, lambda: raid().get("detect", False), put_detect, "Raid detection")

        left = int(raid().get("raid_until", 0) - time.time())

        async def raid_mode(interaction: discord.Interaction):
            r = raid()
            now_on = r.get("raid_until", 0) > time.time()
            r["raid_until"] = 0 if now_on else time.time() + 600
            save_data()
            await self._audit(interaction, f"Raid mode turned **{'off' if now_on else 'on (10 minutes)'}**")
            await self._redraw(interaction)

        self._button(f"Raid mode: ON ({math.ceil(left / 60)}m left)" if left > 0 else "Raid mode: OFF",
                     discord.ButtonStyle.danger if left > 0 else discord.ButtonStyle.secondary, 1, raid_mode)

        def put_age(value):
            raid()["min_age_days"] = value

        self._cycle("Min account age", 1, MIN_AGE_STEPS, lambda: raid().get("min_age_days", 0), put_age,
                    lambda v: f"{v}d" if v else "off", "Minimum account age")

        def put_action(value):
            honeypot_cfg(gid)["action"] = value

        self._cycle("Honeypot action", 1, list(HONEYPOT_ACTIONS), lambda: honeypot_cfg(gid)["action"], put_action,
                    lambda v: v, "The honeypot action")

    # ───────────── 👥 members & roles ─────────────
    def _embed_members(self) -> discord.Embed:
        g = self.guild
        cfg = get_config(g.id)
        st = sticky_cfg(g.id)
        e = make_embed("👥 Members & roles")
        e.add_field(name="🔁 Sticky roles", value=f"{onoff(st['enabled'])} · {len(data['sticky'].get(str(g.id), {}))} members remembered")
        e.add_field(name="Restores", value=" ".join(f"<@&{r}>" for r in st["roles"]) or "every safe role (no allow-list)")
        auto = g.get_role(cfg.get("autorole", 0))
        e.add_field(name="🎭 Autorole", value=auto.mention if auto else "none", inline=False)
        e.set_footer(text="Pick a role below to make it the autorole (given to every new member). Allow-list: /stickyroles")
        return e

    def _controls_members(self) -> None:
        gid = self.guild.id
        self._toggle("Sticky roles", 1, lambda: sticky_cfg(gid)["enabled"],
                     lambda v: sticky_cfg(gid).__setitem__("enabled", v), "Sticky roles")

        async def clear_autorole(interaction: discord.Interaction):
            get_config(gid)["autorole"] = 0
            save_data()
            await self._audit(interaction, "Autorole **cleared**")
            await self._redraw(interaction)

        self._button("Clear autorole", discord.ButtonStyle.secondary, 1, clear_autorole,
                     disabled=not get_config(gid).get("autorole"))

        picker = discord.ui.RoleSelect(placeholder="Set the autorole…", min_values=1, max_values=1, row=2)

        async def chosen(interaction: discord.Interaction):
            role = picker.values[0]
            problem = role_error(interaction, role)
            if not problem and (role.is_default() or role.managed or is_dangerous_role(role)):
                problem = "That role can't be an autorole (it's @everyone, managed, or has moderator permissions)."
            if problem:
                return await interaction.response.send_message(f"**{role.name}**: {problem}", ephemeral=True)
            get_config(gid)["autorole"] = role.id
            save_data()
            await self._audit(interaction, f"Autorole set to {role.mention}")
            await self._redraw(interaction)

        picker.callback = chosen
        self.add_item(picker)

    # ───────────── 📜 logging ─────────────
    def _embed_logging(self) -> discord.Embed:
        g = self.guild
        cfg = get_config(g.id)
        modlog = g.get_channel(cfg.get("modlog", 0))
        e = make_embed("📜 Logging", "The mod log records kicks, bans, timeouts, warnings, automod hits, deleted/edited "
                       "messages, joins, leaves, honeypot catches, reports and every change made in this panel.")
        e.add_field(name="Mod log channel", value=modlog.mention if modlog else "❌ not set")
        tlog = g.get_channel(ticket_cfg(g.id).get("log", 0))
        e.add_field(name="Ticket transcripts", value=tlog.mention if tlog else "not set")
        e.set_footer(text="Pick a channel below to use it as the mod log. I need View, Send and Embed Links there.")
        return e

    def _controls_logging(self) -> None:
        gid = self.guild.id
        have = isinstance(self.guild.get_channel(get_config(gid).get("modlog", 0)), discord.TextChannel)

        async def turn_off(interaction: discord.Interaction):
            await self._audit(interaction, "Mod log **turned off**")  # last entry goes to the old channel
            get_config(gid)["modlog"] = 0
            save_data()
            await self._redraw(interaction)

        async def test(interaction: discord.Interaction):
            await log_action(self.guild, "🛠️ Test log entry", f"Requested by {interaction.user.mention}.", discord.Color.blurple())
            await interaction.response.send_message("Sent a test entry to the mod log.", ephemeral=True)

        self._button("Turn mod log off", discord.ButtonStyle.secondary, 1, turn_off, disabled=not have)
        self._button("Send test entry", discord.ButtonStyle.secondary, 1, test, disabled=not have)

        picker = discord.ui.ChannelSelect(
            placeholder="Choose the mod log channel…", channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1, row=2,
        )

        async def chosen(interaction: discord.Interaction):
            channel = interaction.guild.get_channel(picker.values[0].id)
            if not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message("That isn't a text channel I can use.", ephemeral=True)
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message(
                    f"I can't post in {channel.mention} — give me **View Channel**, **Send Messages** and **Embed Links** there.",
                    ephemeral=True,
                )
            get_config(gid)["modlog"] = channel.id
            save_data()
            await self._audit(interaction, f"Mod log set to {channel.mention}")
            await self._redraw(interaction)

        picker.callback = chosen
        self.add_item(picker)

    # ───────────── 🎫 tickets & modmail ─────────────
    def _embed_support(self) -> discord.Embed:
        g = self.guild
        cfg = get_config(g.id)
        e = make_embed("🎫 Tickets & modmail")
        tk = ticket_cfg(g.id)
        if tk.get("category"):
            staff = g.get_role(tk.get("staff_role", 0))
            tlog = g.get_channel(tk.get("log", 0))
            open_now = sum(1 for t in data["tickets"].values() if t["guild"] == g.id)
            e.add_field(
                name="🎫 Tickets",
                value=(f"✅ {open_now} open\nStaff: {staff.mention if staff else '—'}\nTranscripts: {tlog.mention if tlog else '—'}\n"
                       f"Max open per member: {tk.get('max_open', 1)}\nStaff thread: {onoff(tk.get('staff_thread', True))}"),
            )
        else:
            e.add_field(name="🎫 Tickets", value="❌ not set up — run `/ticket-setup`")
        mm_cat = g.get_channel(cfg.get("modmail_category", 0))
        if isinstance(mm_cat, discord.CategoryChannel):
            mm_open = sum(1 for t in data["modmail"].values() if t["guild"] == g.id)
            log = g.get_channel(cfg.get("modmail_log", 0))
            e.add_field(name="📬 Modmail", value=(f"✅ {mm_open} open\nCategory: {mm_cat.name}\n"
                                                   f"Log: {log.mention if log else '—'}\nBlocked users: {len(cfg.get('blocked', []))}"))
        else:
            e.add_field(name="📬 Modmail", value="❌ not set up — run `/modmail-setup`")
        e.set_footer(text="Setup commands: /ticket-setup · /modmail-setup")
        return e

    def _controls_support(self) -> None:
        gid = self.guild.id
        configured = bool(ticket_cfg(gid).get("category"))

        def put(key):
            return lambda value: get_config(gid).setdefault("ticket", {}).__setitem__(key, value)

        self._toggle("Ticket staff thread", 1, lambda: ticket_cfg(gid).get("staff_thread", True), put("staff_thread"),
                     "The ticket staff thread", disabled=not configured)
        self._cycle("Max open tickets", 1, MAX_OPEN_STEPS, lambda: ticket_cfg(gid).get("max_open", 1), put("max_open"),
                    str, "Max open tickets per member", disabled=not configured)


@bot.tree.command(name="adminpanel", description="Open the admin control panel (only you can see it)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def adminpanel(interaction: discord.Interaction):
    view = AdminPanel(interaction.user, interaction.guild)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
    view.origin = interaction


# ══════════════════════════════════════════════════════════════
# 🧑‍⚖️ /admin @member — pick a moderation action, then fill in a form (how long, why…)
# ══════════════════════════════════════════════════════════════
MOD_ACTIONS = {
    # key: (emoji, label, description)
    "warn": ("⚠️", "Warn", "Add a warning (can trigger auto-punishments)"),
    "warnings": ("📋", "View warnings", "See their warning history"),
    "clearwarnings": ("🧽", "Clear warnings", "Wipe all of their warnings"),
    "timeout": ("🔇", "Timeout", "Mute them for a set time"),
    "untimeout": ("🔊", "Remove timeout", "End their timeout now"),
    "kick": ("👢", "Kick", "Remove them (they can rejoin)"),
    "ban": ("🔨", "Ban", "Ban them permanently"),
    "tempban": ("⏳", "Temporary ban", "Ban them, then unban automatically"),
    "softban": ("🧹", "Softban", "Ban + unban to wipe their last day of messages"),
    "nickname": ("✏️", "Change nickname", "Set or reset their nickname"),
    "purge": ("🗑️", "Delete their messages", "Remove their recent messages in this channel"),
}
NEEDS_HIERARCHY = {"warn", "timeout", "untimeout", "kick", "ban", "tempban", "softban", "nickname"}
MAX_TIMEOUT = timedelta(days=28)
MIN_DURATION = timedelta(minutes=1)
DISCORD_REFUSED = (
    "❌ Discord refused that. I may be missing a permission, or this member can't be moderated that way "
    "(for example, timeouts don't work on Administrators)."
)


def fmt_duration(delta: timedelta) -> str:
    secs, parts = int(delta.total_seconds()), []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            n, secs = divmod(secs, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) or "0s"


def clamp_fields(embed: discord.Embed) -> discord.Embed:
    """Discord rejects embed fields over 1024 characters — never let that break a panel."""
    for i, f in enumerate(embed.fields):
        if len(f.value) > 1024:
            embed.set_field_at(i, name=f.name, value=f.value[:1020] + " …", inline=f.inline)
    return embed


def timeout_until(member: discord.Member):
    until = getattr(member, "timed_out_until", None)
    return until if until and until > discord.utils.utcnow() else None


class ActionModal(discord.ui.Modal):
    """The form shown after picking an action: how long, how many days, why…"""

    def __init__(self, panel: "MemberActions", action: str):
        emoji, label, _ = MOD_ACTIONS[action]
        super().__init__(title=f"{label}: {panel.target.display_name}"[:45], timeout=300)
        self.panel, self.action = panel, action
        self.inputs: dict[str, discord.ui.TextInput] = {}
        if action in ("timeout", "tempban"):
            self._add("duration", "How long?", default="1h" if action == "timeout" else "1d",
                      placeholder="e.g. 30m, 12h, 3d, 1w2d", max_length=20)
        if action == "ban":
            self._add("days", "Delete messages from last N days (0-7)", default="0", max_length=1)
        if action == "nickname":
            self._add("nick", "New nickname (empty = reset)", default=panel.target.nick or "", required=False, max_length=32)
        if action == "purge":
            self._add("scan", "How many recent messages to scan (1-500)", default="100", max_length=3)
        if action in ("warn", "clearwarnings", "timeout", "kick", "ban", "tempban", "softban"):
            self._add("reason", "Reason" + ("" if action == "warn" else " (optional)"), style=discord.TextStyle.paragraph,
                      required=action == "warn", max_length=300, placeholder="Why? This is saved in the logs.")

    def _add(self, key: str, label: str, *, default: str = "", required: bool = True, **kw) -> None:
        field = discord.ui.TextInput(label=label[:45], default=default or None, required=required, **kw)
        self.inputs[key] = field
        self.add_item(field)

    def _text(self, key: str) -> str:
        field = self.inputs.get(key)
        return (field.value or "").strip() if field else ""

    def parse(self) -> dict:
        """Turn the form into action parameters, or raise ValueError with a message for the admin."""
        params: dict = {"reason": self._text("reason")}
        if self.action == "warn" and not params["reason"]:
            raise ValueError("A warning needs a reason.")
        if "duration" in self.inputs:
            delta = parse_duration(self._text("duration"))
            if delta is None:
                raise ValueError("I couldn't read that time. Try `30m`, `12h`, `3d` or `1w2d`.")
            if delta < MIN_DURATION:
                raise ValueError("The time must be at least 1 minute.")
            if self.action == "timeout" and delta > MAX_TIMEOUT:
                raise ValueError("Discord timeouts can last at most 28 days. For longer, use a temporary ban.")
            params["duration"] = delta
        if "days" in self.inputs:
            text = self._text("days")
            if not text.isdigit() or not 0 <= int(text) <= 7:
                raise ValueError("Days of messages to delete must be a number from 0 to 7.")
            params["days"] = int(text)
        if "scan" in self.inputs:
            text = self._text("scan")
            if not text.isdigit() or not 1 <= int(text) <= 500:
                raise ValueError("The number of messages to scan must be from 1 to 500.")
            params["scan"] = int(text)
        if "nick" in self.inputs:
            params["nick"] = self._text("nick") or None
        return params

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            params = self.parse()
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await self.panel.run_action(interaction, self.action, **params)


class MemberActions(discord.ui.View):
    def __init__(self, admin: discord.Member, target: discord.Member, guild: discord.Guild):
        super().__init__(timeout=600)
        self.admin, self.target, self.guild = admin, target, guild
        self.origin: discord.Interaction | None = None
        self._build()

    # ───────────── state shown on the panel ─────────────
    def _problem(self) -> str | None:
        return hierarchy_problem(self.guild, self.admin, self.target)

    def _warnings(self) -> list[dict]:
        return data["warnings"].get(str(self.guild.id), {}).get(str(self.target.id), [])

    def embed(self) -> discord.Embed:
        t = self.target
        e = make_embed(f"🧑‍⚖️ Moderate {t.display_name}",
                       "Pick an action from the menu — you'll fill in the details (like how long) in a form.")
        e.set_thumbnail(url=t.display_avatar.url)
        e.add_field(name="ID", value=str(t.id))
        e.add_field(name="Account created", value=discord.utils.format_dt(t.created_at, "R"))
        e.add_field(name="Joined", value=discord.utils.format_dt(t.joined_at, "R") if t.joined_at else "—")
        roles = " ".join(r.mention for r in reversed(t.roles) if not r.is_default())
        e.add_field(name="Roles", value=roles or "none", inline=False)
        warns = self._warnings()
        e.add_field(name="Warnings", value=f"{len(warns)}" + (f"\nLatest: {warns[-1]['reason'][:80]}" if warns else ""))
        until = timeout_until(t)
        e.add_field(name="Timeout", value=f"until {discord.utils.format_dt(until, 'R')}" if until else "—")
        e.add_field(name="Nickname", value=t.nick or "—")
        problem = self._problem()
        if problem:
            e.add_field(name="⚠️ Limited", value=f"{problem}\nYou can only view their warnings.", inline=False)
        e.set_footer(text="Only you can see this panel.")
        return clamp_fields(e)

    def _gone_embed(self) -> discord.Embed:
        return make_embed(f"🧑‍⚖️ {self.target.display_name}", "They're no longer in the server, so there's nothing more to do here.")

    # ───────────── components ─────────────
    def _options(self) -> list[discord.SelectOption]:
        keys = ["warnings"] if self._problem() else list(MOD_ACTIONS)
        out = []
        for key in keys:
            if key == "untimeout" and not timeout_until(self.target):
                continue
            if key == "clearwarnings" and not self._warnings():
                continue
            emoji, label, desc = MOD_ACTIONS[key]
            if key == "clearwarnings":
                desc = f"Wipe all {len(self._warnings())} of their warnings"
            out.append(discord.SelectOption(label=label, value=key, emoji=emoji, description=desc))
        return out

    def _build(self) -> None:
        self.clear_items()
        menu = discord.ui.Select(placeholder="Choose a moderation action…", options=self._options(), row=0)

        async def pick(interaction: discord.Interaction):
            action = menu.values[0]
            if action == "warnings":
                await self._show_warnings(interaction)
                await self._reset_menu()
            elif action == "untimeout":
                await self.run_action(interaction, action, reason="")
            else:
                await interaction.response.send_modal(ActionModal(self, action))
                await self._reset_menu()

        async def close(interaction: discord.Interaction):
            self.stop()
            await interaction.response.edit_message(content="Closed.", embed=None, view=None)

        menu.callback = pick
        self.add_item(menu)
        close_button = discord.ui.Button(label="Close", emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
        close_button.callback = close
        self.add_item(close_button)

    async def _reset_menu(self) -> None:
        """Re-render the panel so the menu is no longer showing the choice that was just made."""
        self._build()
        if self.origin:
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin.id:
            await interaction.response.send_message("This isn't your panel — run `/admin` yourself.", ephemeral=True)
            return False
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        if not member.guild_permissions.administrator:
            await interaction.response.send_message("You no longer have the Administrator permission.", ephemeral=True)
            self.stop()
            return False
        return True

    async def on_timeout(self) -> None:
        if self.origin:
            try:
                await self.origin.edit_original_response(content="⌛ This panel expired — run `/admin` again.", view=None)
            except discord.HTTPException:
                pass

    # ───────────── view warnings ─────────────
    async def _show_warnings(self, interaction: discord.Interaction) -> None:
        warns = self._warnings()
        if not warns:
            return await interaction.response.send_message(f"**{self.target.display_name}** has no warnings.", ephemeral=True)
        first = max(len(warns) - 9, 1)
        lines = [f"**{i}.** {w['reason'][:150]} — <@{w['mod']}>" for i, w in enumerate(warns[-10:], first)]
        embed = make_embed(f"📋 Warnings for {self.target.display_name} ({len(warns)})", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ───────────── running an action ─────────────
    async def run_action(self, interaction: discord.Interaction, action: str, reason: str = "", **params) -> None:
        guild, target = self.guild, self.target
        if guild.get_member(target.id) is None:
            return await interaction.response.send_message("❌ That member isn't in the server any more.", ephemeral=True)
        if action in NEEDS_HIERARCHY:
            problem = hierarchy_problem(guild, interaction.user, target)
            if problem:
                return await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        await interaction.response.defer()
        why = reason.strip() or "No reason given"
        try:
            result, gone = await getattr(self, f"_do_{action}")(interaction, why, **params)
        except discord.HTTPException:
            return await interaction.followup.send(DISCORD_REFUSED, ephemeral=True)
        if not gone:
            try:
                self.target = await guild.fetch_member(target.id)
            except discord.NotFound:
                gone = True
            except discord.HTTPException:
                pass
        if gone:
            self.stop()
            return await interaction.edit_original_response(content=f"✅ {result}", embed=self._gone_embed(), view=None)
        self._build()
        await interaction.edit_original_response(content=f"✅ {result}", embed=self.embed(), view=self)

    def _audit_reason(self, interaction: discord.Interaction, why: str) -> str:
        return f"{interaction.user}: {why}"

    async def _do_warn(self, interaction, why, **_):
        guild, target = self.guild, self.target
        count = add_warning(guild, target, why, interaction.user.id)
        try:
            await target.send(f"You were warned in **{guild.name}**: {why}")
        except discord.HTTPException:
            pass
        escalation = await apply_escalation(guild, target, count)
        await log_action(
            guild, "⚠️ Warning issued", f"{target.mention} (`{target.id}`)\n**By:** {interaction.user.mention}",
            discord.Color.gold(), [("Reason", why), ("Total warnings", count)],
        )
        result = f"Warned **{target.display_name}** — {why} (total: {count})"
        if escalation:
            result += f"\n{escalation}"
        return result, bool(escalation and ("**banned**" in escalation or "**kicked**" in escalation))

    async def _do_clearwarnings(self, interaction, why, **_):
        guild, target = self.guild, self.target
        n = len(self._warnings())
        data["warnings"].get(str(guild.id), {}).pop(str(target.id), None)
        save_data()
        await log_action(guild, "🧽 Warnings cleared", f"{target.mention} — by {interaction.user.mention}",
                         discord.Color.green(), [("Cleared", f"{n} warning(s)"), ("Reason", why)])
        return f"Cleared {n} warning(s) for **{target.display_name}**.", False

    async def _do_timeout(self, interaction, why, duration, **_):
        await self.target.timeout(duration, reason=self._audit_reason(interaction, why))
        return f"Timed out **{self.target.display_name}** for **{fmt_duration(duration)}** — {why}", False

    async def _do_untimeout(self, interaction, why, **_):
        await self.target.timeout(None, reason=self._audit_reason(interaction, "Timeout removed via /admin"))
        return f"Removed the timeout from **{self.target.display_name}**.", False

    async def _do_kick(self, interaction, why, **_):
        await self.target.kick(reason=self._audit_reason(interaction, why))
        return f"Kicked **{self.target.display_name}** — {why}", True

    async def _do_ban(self, interaction, why, days=0, **_):
        await self.target.ban(reason=self._audit_reason(interaction, why), delete_message_seconds=days * 86400)
        data["tempbans"].pop(f"{self.guild.id}:{self.target.id}", None)
        extra = f" (deleted their last {days} day(s) of messages)" if days else ""
        return f"Banned **{self.target.display_name}** — {why}{extra}", True

    async def _do_tempban(self, interaction, why, duration, **_):
        guild, target = self.guild, self.target
        try:
            await target.send(f"You've been temporarily banned from **{guild.name}** for **{fmt_duration(duration)}**.\nReason: {why}")
        except discord.HTTPException:
            pass
        await target.ban(reason=self._audit_reason(interaction, f"{why} (tempban {fmt_duration(duration)})"))
        data["tempbans"][f"{guild.id}:{target.id}"] = time.time() + duration.total_seconds()
        save_data()
        ends = discord.utils.format_dt(discord.utils.utcnow() + duration, "R")
        return f"Temporarily banned **{target.display_name}** for **{fmt_duration(duration)}** (unbanned {ends}) — {why}", True

    async def _do_softban(self, interaction, why, **_):
        await self.target.ban(reason=self._audit_reason(interaction, f"softban — {why}"), delete_message_seconds=86400)
        await self.guild.unban(self.target, reason="Softban")
        return f"Softbanned **{self.target.display_name}** (last 24h of messages deleted) — {why}", True

    async def _do_nickname(self, interaction, why, nick=None, **_):
        old = self.target.nick
        await self.target.edit(nick=nick, reason=f"Changed via /admin by {interaction.user}")
        await log_action(self.guild, "✏️ Nickname changed", f"{self.target.mention} — by {interaction.user.mention}",
                         discord.Color.blue(), [("Before", old or "—"), ("After", nick or "(reset)")])
        return (f"Set **{self.target.display_name}**'s nickname to **{nick}**." if nick
                else f"Reset **{self.target.display_name}**'s nickname."), False

    async def _do_purge(self, interaction, why, scan=100, **_):
        target = self.target
        deleted = await interaction.channel.purge(limit=scan, check=lambda m: m.author.id == target.id)
        await log_action(self.guild, "🧹 Messages deleted", f"{target.mention}'s messages in {interaction.channel.mention} — by {interaction.user.mention}",
                         discord.Color.red(), [("Deleted", f"{len(deleted)} (scanned the last {scan})")])
        return f"Deleted {len(deleted)} message(s) from **{target.display_name}** in {interaction.channel.mention}.", False


@bot.tree.command(name="admin", description="Moderate a member — pick an action, then fill in the form")
@app_commands.describe(member="The member to moderate")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def admin_cmd(interaction: discord.Interaction, member: discord.Member):
    view = MemberActions(interaction.user, member, interaction.guild)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
    view.origin = interaction


# ══════════════════════════════════════════════════════════════
# 🏆 LEADERBOARDS — XP and Coins, with tabs, pages and your own rank
# XP is per server. Coins are shared across servers, so the coin board only lists this server's members.
# ══════════════════════════════════════════════════════════════
LB_PAGE_SIZE = 10
LB_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
LB_KINDS = {"xp": ("⭐", "XP"), "coins": ("🪙", "Coins")}


def leaderboard_rows(guild: discord.Guild, kind: str) -> list[tuple[discord.Member, int]]:
    """Members of this server ranked by XP or coins (highest first). Bots, people who left and zeros are skipped."""
    if kind == "xp":
        pairs = list(data["xp"].get(str(guild.id), {}).items())
    else:
        pairs = [(uid, acct.get("bal", 0)) for uid, acct in data["economy"].items()]
    rows = []
    for uid, value in pairs:
        if value <= 0:
            continue
        member = guild.get_member(int(uid))
        if member is None or member.bot:
            continue
        rows.append((member, value))
    rows.sort(key=lambda r: (-r[1], r[0].id))
    return rows


class LeaderboardView(discord.ui.View):
    def __init__(self, user: discord.abc.User, guild: discord.Guild, kind: str = "xp"):
        super().__init__(timeout=180)
        self.user, self.guild, self.kind, self.page = user, guild, kind, 0
        self.message: discord.Message | None = None

    def render(self) -> discord.Embed:
        """Build the embed for the current tab/page and refresh the buttons to match."""
        rows = leaderboard_rows(self.guild, self.kind)
        pages = max(1, math.ceil(len(rows) / LB_PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        self._build(pages)

        emoji, label = LB_KINDS[self.kind]
        embed = make_embed(f"{emoji} {label} leaderboard — {self.guild.name}", color=discord.Color.gold())
        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.url)
        if not rows:
            embed.description = ("Nobody has any XP yet — start chatting!" if self.kind == "xp"
                                 else "Nobody has any coins yet — try `/daily`!")
            return embed
        start = self.page * LB_PAGE_SIZE
        lines = ["*Showing members of this server.*"]
        for i, (member, value) in enumerate(rows[start:start + LB_PAGE_SIZE], start + 1):
            name = discord.utils.escape_markdown(member.display_name)[:32]
            amount = f"level {level_for(value)} · {value:,} XP" if self.kind == "xp" else f"{value:,} coins"
            lines.append(f"{LB_MEDALS.get(i, f'`{i}.`')} **{name}** — {amount}")
        embed.description = "\n".join(lines)
        mine = next((i for i, (m, _) in enumerate(rows, 1) if m.id == self.user.id), None)
        you = (f"You're #{mine} of {len(rows)}" if mine
               else "You're not on this board yet" + (" — chat to earn XP!" if self.kind == "xp" else " — try /daily!"))
        embed.set_footer(text=f"Page {self.page + 1}/{pages} · {you}")
        return embed

    def _build(self, pages: int) -> None:
        self.clear_items()
        for kind, (emoji, label) in LB_KINDS.items():
            button = discord.ui.Button(label=label, emoji=emoji, row=0,
                                       style=discord.ButtonStyle.primary if kind == self.kind else discord.ButtonStyle.secondary)
            button.callback = self._switch(kind)
            self.add_item(button)
        for text, step in (("◀", -1), ("▶", 1)):
            button = discord.ui.Button(label=text, row=1, style=discord.ButtonStyle.secondary,
                                       disabled=(self.page + step < 0 or self.page + step >= pages))
            button.callback = self._turn(step)
            self.add_item(button)

    def _switch(self, kind: str):
        async def callback(interaction: discord.Interaction):
            self.kind, self.page = kind, 0
            await interaction.response.edit_message(embed=self.render(), view=self)
        return callback

    def _turn(self, step: int):
        async def callback(interaction: discord.Interaction):
            self.page += step
            await interaction.response.edit_message(embed=self.render(), view=self)
        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Run `/leaderboard` yourself to browse.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def open_leaderboard(interaction: discord.Interaction, kind: str) -> None:
    view = LeaderboardView(interaction.user, interaction.guild, kind)
    await interaction.response.send_message(embed=view.render(), view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="leaderboard", description="See who has the most XP or coins in this server")
@app_commands.describe(board="Which leaderboard to open (default: XP) — you can switch with the buttons")
@app_commands.choices(board=[app_commands.Choice(name="XP", value="xp"), app_commands.Choice(name="Coins", value="coins")])
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction, board: app_commands.Choice[str] | None = None):
    await open_leaderboard(interaction, board.value if board else "xp")


# ══════════════════════════════════════════════════════════════
# ➖ PREFIX COMMANDS  (-ping, -kick @user spamming, -automod rule spam on, …)
# Every slash command also works with a prefix. Arguments are read from the message
# and the command runs through the same code (and the same permission checks).
# ══════════════════════════════════════════════════════════════
PREFIX = os.getenv("BOT_PREFIX", "-")
PRIVATE_COMMANDS = {"password"}  # private replies go to DMs instead of the channel
_SEND_KWARGS = {"embed", "embeds", "view", "allowed_mentions", "file", "files", "delete_after"}
_TRUE_WORDS = {"true", "yes", "y", "on", "1", "enable", "enabled"}
_FALSE_WORDS = {"false", "no", "n", "off", "0", "disable", "disabled"}
_ARG_RE = re.compile(
    r'(?P<key>[A-Za-z_]+)=(?:"(?P<kq>[^"]*)"|(?P<kv>\S+))'  # name=value / name="a value"
    r'|"(?P<q>[^"]*)"|“(?P<sq>[^”]*)”|(?P<w>\S+)'
)


class PrefixUsageError(Exception):
    pass


class _PrefixResponse:
    def __init__(self, ix: "PrefixInteraction"):
        self._ix = ix
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._done = True
        if ephemeral:
            await self._ix._hide_invocation()

    async def send_message(self, content=None, *, ephemeral: bool = False, **kwargs) -> None:
        self._done = True
        self._ix._last = await self._ix._deliver(content, ephemeral, kwargs)


class _PrefixFollowup:
    def __init__(self, ix: "PrefixInteraction"):
        self._ix = ix

    async def send(self, content=None, *, ephemeral: bool = False, **kwargs):
        msg = await self._ix._deliver(content, ephemeral, kwargs)
        if self._ix._last is None:
            self._ix._last = msg
        return msg


class PrefixInteraction:
    """Behaves like a discord.Interaction for our commands, but is built from a normal chat message.

    "Ephemeral" replies can't exist in a normal channel, so they are emulated: the command message is
    deleted straight away and the answer is a short-lived message that pings the author
    (or a DM for private commands such as password).
    """

    def __init__(self, message: discord.Message, command: app_commands.Command):
        self.invocation = message
        self.command = command
        self.client = bot
        self.id = message.id
        self.created_at = message.created_at
        self.guild = message.guild
        self.guild_id = message.guild.id
        self.channel = message.channel
        self.channel_id = message.channel.id
        self.user = message.author
        self.permissions = message.channel.permissions_for(message.author)
        self.response = _PrefixResponse(self)
        self.followup = _PrefixFollowup(self)
        self._last: discord.Message | None = None
        self._hidden = False

    async def original_response(self):
        return self._last

    async def edit_original_response(self, **kwargs):
        if self._last:
            return await self._last.edit(**kwargs)

    async def delete_original_response(self):
        if self._last:
            await self._last.delete()

    async def _hide_invocation(self) -> None:
        if self._hidden:
            return
        self._hidden = True
        try:
            await self.invocation.delete()
        except discord.HTTPException:
            pass

    async def _deliver(self, content, ephemeral: bool, kwargs: dict):
        kwargs = {k: v for k, v in kwargs.items() if k in _SEND_KWARGS and v is not None}
        author, channel = self.user, self.channel
        if ephemeral:
            await self._hide_invocation()
            if self.command.qualified_name in PRIVATE_COMMANDS:
                try:
                    dm_kwargs = {k: v for k, v in kwargs.items() if k not in ("allowed_mentions", "delete_after")}
                    return await author.send(content, **dm_kwargs)
                except discord.HTTPException:
                    return await channel.send(
                        f"{author.mention} I couldn't DM you — turn on DMs from server members and try again.",
                        delete_after=15,
                    )
            kwargs.setdefault("delete_after", 20)
            kwargs.setdefault("allowed_mentions", discord.AllowedMentions(users=[author]))
            text = f"{author.mention} {content}" if content else author.mention
            return await channel.send(text, **kwargs)
        return await channel.send(
            content, reference=self.invocation.to_reference(fail_if_not_exists=False), mention_author=False, **kwargs
        )


def _usage(cmd: app_commands.Command) -> str:
    parts = [PREFIX + cmd.qualified_name]
    for p in cmd.parameters:
        label = p.display_name
        if p.choices and len(p.choices) <= 6:
            label = "|".join(str(c.value) for c in p.choices)
        parts.append(f"<{label}>" if p.required else f"[{label}]")
    return " ".join(parts)


def _unwrap(annotation):
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if args:
            return args[0]
    return annotation


async def _convert(ctx, cmd: app_commands.Command, param, token: str):
    T = discord.AppCommandOptionType
    if param.choices:
        t = token.lower()
        for c in param.choices:
            if t == str(c.value).lower() or t == c.name.lower():
                return c
        for c in param.choices:
            if c.name.lower().startswith(t):
                return c
        raise commands.BadArgument(
            f"`{token}` isn't a valid option. Choose from: " + ", ".join(f"`{c.value}`" for c in param.choices)
        )
    kind = param.type
    if kind == T.string:
        return token
    if kind in (T.integer, T.number):
        try:
            number = int(token) if kind == T.integer else float(token)
        except ValueError:
            raise commands.BadArgument(f"`{token}` isn't a valid number.")
        lo, hi = param.min_value, param.max_value
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            raise commands.BadArgument(f"`{param.display_name}` must be between {lo} and {hi}.")
        return number
    if kind == T.boolean:
        word = token.lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise commands.BadArgument(f"`{token}` isn't a yes/no value — use `on` or `off`.")
    annotation = _unwrap(cmd.callback.__annotations__.get(param.name))
    if kind == T.user:
        if annotation is discord.Member:
            return await commands.MemberConverter().convert(ctx, token)
        try:
            return await commands.MemberConverter().convert(ctx, token)
        except commands.BadArgument:
            return await commands.UserConverter().convert(ctx, token)
    if kind == T.role:
        return await commands.RoleConverter().convert(ctx, token)
    if kind == T.channel:
        converter = {
            discord.TextChannel: commands.TextChannelConverter,
            discord.CategoryChannel: commands.CategoryChannelConverter,
            discord.VoiceChannel: commands.VoiceChannelConverter,
        }.get(annotation, commands.GuildChannelConverter)
        return await converter().convert(ctx, token)
    raise commands.BadArgument(f"I can't read `{param.display_name}` from a text message — use the slash command.")


async def _parse_args(ctx, cmd: app_commands.Command, rest: str, message: discord.Message) -> dict:
    T = discord.AppCommandOptionType
    params = list(cmd.parameters)
    by_name = {}
    for p in params:
        by_name[p.name.lower()] = p
        by_name[p.display_name.lower()] = p

    named: dict[str, str] = {}
    positional: list[tuple[str, int]] = []
    for m in _ARG_RE.finditer(rest):
        if m["key"] and m["key"].lower() in by_name:
            value = m["kq"] if m["kq"] is not None else m["kv"]
            named[by_name[m["key"].lower()].name] = value
        elif m["key"]:
            positional.append((m.group(0), m.start()))
        else:
            positional.append((next(g for g in (m["q"], m["sq"], m["w"]) if g is not None), m.start()))

    attachments = list(message.attachments)
    text_params = [p for p in params if p.type != T.attachment]
    last_param = text_params[-1] if text_params else None
    kwargs: dict = {}
    pos = 0
    for p in params:
        if p.type == T.attachment:
            if attachments:
                kwargs[p.name] = attachments.pop(0)
            elif p.required:
                raise PrefixUsageError(f"`{p.display_name}` needs a file attached to your message.")
            continue
        if p.name in named:
            raw = named[p.name]
        elif pos < len(positional):
            if p is last_param and p.type == T.string:  # last text option swallows the rest of the line
                raw = rest[positional[pos][1]:].strip()
                if len(raw) > 1 and raw[0] == raw[-1] == '"':
                    raw = raw[1:-1]
                pos = len(positional)
            else:
                raw = positional[pos][0]
                pos += 1
        elif p.required:
            raise PrefixUsageError(f"You're missing `{p.display_name}`.")
        else:
            continue
        kwargs[p.name] = await _convert(ctx, cmd, p, raw)
    return kwargs


async def _run_checks(cmd: app_commands.Command, ix: PrefixInteraction) -> None:
    for check in cmd.checks:
        result = check(ix)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise app_commands.CheckFailure("A check failed.")


async def run_prefix_command(message: discord.Message) -> bool:
    """Runs `-command args` if the message is one. Returns True if the message was handled."""
    text = message.content
    if len(text) <= len(PREFIX) or not text.startswith(PREFIX) or text[len(PREFIX)].isspace():
        return False
    parts = text[len(PREFIX):].split(None, 1)
    cmd = bot.tree.get_command(parts[0].lower())
    if cmd is None:
        return False  # not one of our commands — treat as normal chat
    rest = parts[1] if len(parts) > 1 else ""

    ref = message.to_reference(fail_if_not_exists=False)
    if cmd.name in SLASH_ONLY:
        await message.channel.send(
            f"{message.author.mention} **/{cmd.name}** opens a private panel with forms, so it only works as a slash command.",
            reference=ref, mention_author=False, delete_after=15,
        )
        return True
    if isinstance(cmd, app_commands.Group):
        sub_parts = rest.split(None, 1)
        sub = cmd.get_command(sub_parts[0].lower()) if sub_parts else None
        if sub is None:
            options = "|".join(sorted(c.name for c in cmd.commands))
            await message.channel.send(
                f"Usage: `{PREFIX}{cmd.name} <{options}> …`", reference=ref, mention_author=False, delete_after=20
            )
            return True
        cmd, rest = sub, (sub_parts[1] if len(sub_parts) > 1 else "")

    ix = PrefixInteraction(message, cmd)
    try:
        await _run_checks(cmd, ix)  # permissions first, so non-admins learn nothing about the command
        ctx = await bot.get_context(message)
        try:
            kwargs = await _parse_args(ctx, cmd, rest, message)
        except (commands.CommandError, PrefixUsageError) as e:
            await message.channel.send(
                f"❌ {e}\nUsage: `{_usage(cmd)}`", reference=ref, mention_author=False, delete_after=20
            )
            return True
        await cmd.callback(ix, **kwargs)
    except app_commands.AppCommandError as e:
        await on_app_command_error(ix, e)
    except Exception as e:
        await on_app_command_error(ix, app_commands.CommandInvokeError(cmd, e))
    return True


if __name__ == "__main__":
    bot.run(TOKEN)
