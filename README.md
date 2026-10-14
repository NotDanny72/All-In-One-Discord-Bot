In my server (https://discord.gg/9tT5KnzjRS) we used https://bot-hosting.net to host our bot (not sponsored)

# Discord Bot

A slash-command Discord bot with fun, utility, moderation, economy, levels, verification, modmail, auto-moderation and anti-raid features (75+ commands).

## Setup

1. Create an application and bot at https://discord.com/developers/applications
2. Under **Bot**, copy the token and enable both the **Server Members Intent** and the **Message Content Intent**.
3. Copy `.env.example` to `.env` and paste your token in.
4. Install dependencies and run:

   ```
   pip install -r requirements.txt
   python bot.py
   ```
5. Invite the bot with the `bot` and `applications.commands` scopes. Give it these permissions:
   Manage Channels, Manage Roles, Manage Nicknames, Manage Messages, Kick/Ban Members, Moderate Members, Send Messages,
   **View Audit Log** (for the mod log).
   Keep its role above any roles it needs to manage (verified/unverified roles, etc.).

## Quick start

- `/help` lists every command.
- `/verification-setup` posts a verification button panel.
- `/modmail-setup` turns on modmail (members DM the bot to contact staff).
  In a modmail channel, just type to reply to the user (✅ = delivered). Start a message with `//` to keep it internal, use `/anonymous` to toggle anonymous mode for the thread, and `/close` to finish.

## Prefix commands

Every slash command also works with the `-` prefix: `-ping`, `-roll 20 3`, `-kick @user being rude`, `-automod rule spam on`.
Same permissions as the slash version (moderation commands still need Administrator).

- Mention people/roles/channels as usual (or use IDs). The last text option takes the rest of the line, so no quotes are needed for reasons or messages.
- Wrap a multi-word value in quotes when it isn't last: `-poll "Best pet?" cats, dogs`.
- Skip optional options with `name=value`: `-escalation kick_at=5`, `-antiraid detection=on joins=7`.
- Yes/no options accept `on/off`, `true/false`, `yes/no`.
- Usage mistakes get a friendly hint, e.g. `Usage: -kick <member> [reason]`.
- Private ("only you can see this") replies can't exist in a normal channel, so the bot deletes your command message and
  sends a short-lived reply that pings you. `-password` is sent by DM instead.
- Inside a modmail thread, `-close`, `-anonymous` etc. run as commands; any other text is still relayed to the user.
- Change the prefix by adding `BOT_PREFIX=!` (or anything) to your `.env`.
- Needs the **Message Content Intent** (already required for modmail).

## Admin panel

`/adminpanel` (Administrator only, slash command only) opens a private control panel that only you can see.
Pick a section from the menu at the top:

- **📊 Overview** — status of every system (auto-mod, mod log, anti-raid, honeypot, verification, autorole, sticky roles, modmail, tickets, shop, warnings, coins, bot health) plus a **Needs attention** list: no mod log, missing bot permissions, broken honeypot channels, auto-mod on with no rules.
- **🤖 Auto-moderation** — switch the master switch, strikes and each of the six rules on or off.
- **🛡️ Protection** — raid detection, raid mode (10 minutes), minimum account age (cycles off/1/3/7/14/30 days) and the honeypot action.
- **👥 Members & roles** — sticky roles on/off, and pick or clear the autorole from a role menu (risky roles are refused).
- **📜 Logging** — pick the mod log channel from a menu (I check I can post there), send a test entry, or turn it off.
- **🎫 Tickets & modmail** — ticket staff thread on/off and max open tickets; modmail status.

Every change is applied instantly and logged to the mod log ("🛠️ Admin panel: @you turned … on"). The panel expires after 10 minutes of inactivity.
For finer settings (thresholds, word lists, allow-lists) use the dedicated commands the panel points you to.

## Moderate a member with `/admin @member`

`/admin @member` (Administrator only, slash command only) opens a private panel about that member — ID, account age, roles, warnings, timeout status, nickname —
with a menu of every moderation action. Pick one and a **form** pops up asking for the details:

| Action | Form asks for |
|---|---|
| ⚠️ Warn | reason (required) — can trigger your `/escalation` auto-punishments |
| 🔇 Timeout | **how long** (`30m`, `12h`, `3d`, `1w2d`; up to 28 days) + reason |
| ⏳ Temporary ban | **how long** (up to a year) + reason — unbanned automatically |
| 🔨 Ban | days of their messages to delete (0–7) + reason |
| 👢 Kick / 🧹 Softban | reason |
| ✏️ Change nickname | new nickname (empty resets it) |
| 🗑️ Delete their messages | how many recent messages in this channel to scan |
| 🧽 Clear warnings | reason |
| 🔊 Remove timeout, 📋 View warnings | no form — one click |

The menu only offers what makes sense (e.g. *Remove timeout* appears only while they're timed out). Role hierarchy is checked when you open the panel *and* again when you submit.
After an action the panel updates in place so you can keep going; after a kick/ban it closes. Everything is logged to the mod log.

## Permissions

All moderation and configuration commands (kick, ban, warn, purge, automod, modlog, modmail setup, rolemenu, and so on)
require the **Administrator** permission, and are hidden from everyone else in Discord's command menu.
Fun, utility, economy and level commands stay open to everyone.
Modmail threads: who can reply/close is decided by who can see the modmail category, so keep that category staff-only.

## Auto-moderation & protection

**Fastest way:** run `/automod-setup`, pick a preset (Light / Standard / Strict) and optionally a log channel and a trusted role to exempt.
It turns everything on in one go and tells you if the bot is missing any permissions. Your bad-word list, allowed domains and exemptions are kept.

Or set things up by hand:

Everything is off until you turn it on. Administrators and anyone with Manage Messages are always exempt from automod.

1. `/modlog #channel` — logs kicks, bans, timeouts, warnings, automod hits, deleted/edited messages, joins and leaves.
2. `/automod toggle enabled:True`, then switch rules on with `/automod rule`:
   spam, Discord invites, links, bad words, mass mentions, excessive caps.
   Tune them with `/automod spam`, `mentions`, `caps`, `badword`, `linkdomain` (allowed domains), `exempt` (roles/channels).
   `/automod status` shows the current settings.
3. `/escalation` — automatic timeout / kick / ban when someone reaches X warnings.
   Applies to `/warn` and to automod strikes (`/automod strikes` turns strikes off).
4. `/antiraid` — kick joins with very new accounts and auto-detect join floods. `/raidmode` turns lockdown on/off by hand.

Extra moderation: `/tempban` (e.g. `3d`, `12h`, `1w2d`), `/softban`, `/purge-user`, `/unwarn`, `/snipe`,
and right-click a message → Apps → **Report message** (sends it to the mod log).

Roles: `/autorole` (role for every new member) and `/rolemenu` (button panel so members pick their own roles).

## Sticky roles

When a member leaves, the bot remembers their roles and gives them back if they rejoin.

- `/stickyroles toggle enabled:True` turns it on (off by default).
- By default it restores every role the bot can assign **except** @everyone, managed/bot roles, and roles with moderator permissions.
- `/stickyroles add @role` switches to an allow-list: only those roles are restored (even moderator roles, because you chose them). `remove`, `list` and `forget @member` manage it.
- Someone who was verified and rejoins gets their verified role back and skips the verification gate.
- Banned members are forgotten, and memories expire after 60 days away.
- The bot's role must sit above the roles it restores.

## Tickets

1. `/ticket-setup channel:#support category:Tickets staff_role:@Support [log_channel] [max_open]` posts an **Open a ticket** panel.
2. A member clicks it, fills in a short form, and gets a private channel (`ticket-0001`) that only they, the staff role and the bot can see.
3. Staff can **Claim** the ticket. Anyone with access can close it with the **Close** button (with a confirmation).
4. Closing saves a transcript to the log channel, DMs a copy to the member, and deletes the channel.

**Staff thread:** every ticket also gets a private thread (`team-notes-0001`) inside the ticket channel, opened automatically.
Only the staff role (pinged when it's created), people who claim the ticket, and members with Manage Threads can see it — the person who opened the ticket can't.
Use it for internal discussion. Its messages are saved to the log channel as a separate `…-staff-notes.txt` file when the ticket closes, and are **never** included in the copy DM'd to the member.
If you can't see it, run `/ticket notes` in the ticket (it adds you, or creates the thread for older tickets).
Turn it off with `staff_thread:False` in `/ticket-setup`. The bot needs **Create Private Threads** and **Send Messages in Threads**; if it can't make the thread, the ticket still opens normally.

Inside a ticket: `/ticket add @member`, `remove`, `rename`, `notes`, `claim`, `close [reason]` (also `-ticket close`). Staff = the staff role or Administrator.
The bot needs **Manage Channels** and **Manage Roles** to create private ticket channels, and only grants permissions it holds itself. Panels and buttons keep working after restarts.

## Honeypot

A trap channel for spam bots and hacked accounts: anyone who posts in it is dealt with automatically.

1. `/honeypot setup` creates `#do-not-post` at the top of your channel list (or pass an existing `channel:`) and posts a "do not post here" warning.
   Pick what happens with `action:` — **softban** (default: ban then unban, wipes their last 24h of messages, so a hacked account's owner can rejoin after securing it), **ban**, **kick**, or **timeout** (24 hours).
2. When someone posts there: the message is deleted, they get a DM explaining why (if their DMs are open), the punishment is applied, and it's logged to your mod log with their account age and what they posted.
3. Never trapped: the server owner, bots, and anyone with Manage Messages (or a role you exempt with `/honeypot exempt`).
4. `/honeypot add` / `remove` manage extra trap channels, `/honeypot action` changes the punishment, `/honeypot status` shows settings and checks that everything will work.

Needs **Manage Messages** plus **Ban Members** (softban/ban), **Kick Members** or **Moderate Members**, and the bot's role above the people it punishes.
Bots only fall in if the channel is visible to them, so keep it visible to @everyone. If you use verification, remember unverified members can post there too.
It works before automod and prefix commands, so even `-ping` in the trap counts.

## Coin gifts & admin coins

- `/coinsgift @member amount [message] [anonymous]` — gift coins from your own balance with an optional note (`/give` is the plain, no-frills version).
  With `anonymous:True` only you see the confirmation and the public message says "Someone sent…"; staff can still see who it was in the mod log.
- `/admincoins add @member amount [reason]`, `remove` and `set` (Administrator only). Every change is logged to the mod log (who, how much, before → after, why),
  `add` also DMs the member, and `remove` never takes a balance below 0.
- Coins are shared across every server the bot is in, so only give admin rights to people you trust with the bot's economy.

## Leaderboards

`/leaderboard` (or `-leaderboard`) opens an interactive board with **⭐ XP** and **🪙 Coins** tabs. `/leaderboard board:Coins` opens straight on the coin tab.

- 10 people per page with ◀ ▶ buttons, medals for the top 3, and a footer showing **your own rank** ("You're #7 of 42").
- XP shows level and total XP. Coins are shared across servers, but the coin board only lists **members of this server**.
- Bots, people who left, and anyone with 0 are left off. Only the person who opened a board can page through it, and it stops responding after 3 minutes.

## Shop

Members spend coins (from `/daily`, `/work`, `/gamble`, trivia…) on things admins put in the shop.

- `/shop` browses it (with page buttons), `/buy <item> [quantity]` buys, `/inventory` shows what you own, `/redeem <item>` uses one.
- **Role items** give a role the moment they're bought (once per person). **Normal items** go in the inventory; `/redeem` uses one and posts a request to the mod log so staff can hand over the reward (set a mod log with `/modlog`, otherwise redeeming is refused and nothing is lost).
- Pick items by name, `#number` (shown in `/shop`), or the start of a name/word (`/buy cake` finds "Birthday Cake"). `/buy` suggests names as you type.
- Admins (`/shopadmin`): `add name price [role] [stock] [description]`, `remove`, `edit` (name, price, stock, description), `give @member item`.
  `stock` of -1 means unlimited, 0 means sold out. Admin commands need the **exact** item name or `#number`, so a typo can't hit the wrong item.
  Removing an item takes it off sale, but people who already own it can still use it.
- Roles with moderator permissions, managed roles and roles above the bot can't be sold. If the bot can't hand over a role, the buyer is refunded.
- With the prefix: `-buy "Birthday Cake" 2`, `-shopadmin add "Birthday Cake" 100 stock=5 description="Yummy"`.

Data (coins, XP, warnings, config, modmail threads) is stored in `data.json`, created automatically.


