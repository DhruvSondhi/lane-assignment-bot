import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime
import os
import re
import webserver

# -----------------------------
# Bot configuration & intents
# -----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# -----------------------------
# Data & constants
# -----------------------------
# Matches keyed by lane message id
# match = {
#   'message_id', 'channel_id', 'participants': {user_id: {'lane','original_channel','member'}},
#   'start_time', 'guild', 'paused_at', 'total_paused_time'
# }
matches_by_msg = {}           # message_id -> match dict
active_match_by_channel = {}  # channel_id -> message_id (current active in that channel)

# 9 minutes 45 seconds
MATCH_DURATION = 585

LANE_REACTIONS = {
    '🟡': 'Lane - Yellow',
    '🔵': 'Lane - Blue',
    '🟢': 'Lane - Green'
}

# -----------------------------
# Helpers
# -----------------------------
def extract_message_id_or_link(s: str):
    """
    Accepts either a bare numeric ID or a full Discord message link.
    Returns (guild_id, channel_id, message_id) when a link is provided,
    or (None, None, message_id) when only ID is provided.
    """
    s = s.strip().strip('<>').strip()
    # Message link format: https://discord.com/channels/<guild>/<channel>/<message>
    m = re.search(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Bare ID?
    if s.isdigit():
        return None, None, int(s)
    return None, None, None

def channel_allows_control(channel: discord.TextChannel):
    return channel and channel.name.lower() in {"lane-assignment"}

def can_move_members(guild: discord.Guild) -> bool:
    me = guild.me
    return bool(me and me.guild_permissions.move_members)

# -----------------------------
# Events
# -----------------------------
@bot.event
async def on_ready():
    print(f'{bot.user.name} connected. Lane Assignment Bot ready.')
    match_timer.start()

@bot.event
async def on_message(message: discord.Message):
    # ignore bots/DMs
    if message.author.bot or not message.guild:
        return

    content = message.content.lower().strip()

    if not content:
        await bot.process_commands(message)
        return

    # Only react to free-text controls in lane-assignment channels
    if channel_allows_control(message.channel):
        # START
        if ('start match lane assignments' in content) or ('start laning' in content):
            await start_lane_assignment(message)
            await bot.process_commands(message)
            return

        # STOP by active or specific ID/link
        if content.startswith('stop match'):
            # Accept: "stop match", "stop match <id>", "stop match <link>"
            parts = message.content.split(maxsplit=2)
            if len(parts) == 2:
                # just "stop match" -> stop active in this channel
                await stop_match_text(message)
            elif len(parts) == 3:
                await stop_match_text(message, parts[2])
            else:
                await stop_match_text(message)
            await bot.process_commands(message)
            return

        # PAUSE / RESUME
        if ('pause match' in content):
            await pause_match_text(message)
            await bot.process_commands(message)
            return

        if ('resume match' in content):
            await resume_match_text(message)
            await bot.process_commands(message)
            return

        # STATUS
        if ('time remaining' in content) or ('match status' in content):
            await show_match_status_text(message)
            await bot.process_commands(message)
            return

    # allow prefixed commands
    await bot.process_commands(message)

# -----------------------------
# Core flows
# -----------------------------
async def start_lane_assignment(message: discord.Message):
    ch_id = message.channel.id
    guild_id = message.guild.id

    # One active match per channel
    if ch_id in active_match_by_channel:
        await message.reply("❌ There's already an active lane assignment in this channel! Use `stop match` to end it.")
        return

    embed = discord.Embed(
        title="🎯 Lane Assignments Started!",
        description="React with your preferred lane. You'll be moved automatically!\n\n**Match Duration:** 9 minutes 45 seconds",
        color=0xe74c3c
    )
    embed.add_field(name="🟡", value="Lane - Yellow", inline=True)
    embed.add_field(name="🔵", value="Lane - Blue", inline=True)
    embed.add_field(name="🟢", value="Lane - Green", inline=True)
    embed.add_field(name="⚠️ Important", value="You must be in a voice channel to be moved!", inline=False)
    embed.set_footer(text=f"Started by {message.author.display_name}")
    embed.timestamp = datetime.now()

    lane_message = await message.channel.send(embed=embed)
    for emoji in LANE_REACTIONS.keys():
        await lane_message.add_reaction(emoji)

    match = {
        'message_id': lane_message.id,
        'channel_id': message.channel.id,
        'participants': {},
        'start_time': datetime.now(),
        'guild': message.guild,
        'paused_at': None,
        'total_paused_time': 0
    }
    matches_by_msg[lane_message.id] = match
    active_match_by_channel[message.channel.id] = lane_message.id

    await message.reply(f"✅ Lane assignment started! **Match Message ID:** `{lane_message.id}`")

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    message = reaction.message
    guild = message.guild
    if not guild:
        return

    msg_id = message.id
    if msg_id not in matches_by_msg:
        return  # not a lane message

    match = matches_by_msg[msg_id]
    emoji = str(reaction.emoji)
    if emoji not in LANE_REACTIONS:
        return

    member = guild.get_member(user.id)
    if not member or not member.voice or not member.voice.channel:
        temp_msg = await message.channel.send(f"❌ {user.mention}, you must be in a voice channel to join a lane!")
        await asyncio.sleep(5)
        await temp_msg.delete()
        try:
            await reaction.remove(user)
        except discord.HTTPException:
            pass
        return

    original_channel = member.voice.channel
    target_lane = LANE_REACTIONS[emoji]
    target_channel = discord.utils.get(guild.voice_channels, name=target_lane)

    if not target_channel:
        temp_msg = await message.channel.send(f"❌ {target_lane} voice channel not found! Please create it first.")
        await asyncio.sleep(5)
        await temp_msg.delete()
        return

    if not can_move_members(guild):
        await message.channel.send("❌ I don't have **Move Members** permission.")
        return

    # keep only one lane reaction per user
    try:
        me_perms = message.channel.permissions_for(guild.me)
        if me_perms.manage_messages:
            for other_emoji in LANE_REACTIONS:
                if other_emoji != emoji:
                    await message.remove_reaction(other_emoji, user)
    except discord.HTTPException:
        pass

    try:
        await member.move_to(target_channel)
        match['participants'][user.id] = {
            'lane': target_lane,
            'original_channel': original_channel.id,
            'member': member
        }
        confirmation = await message.channel.send(f"✅ {user.mention} assigned to **{target_lane}**!")
        await asyncio.sleep(3)
        await confirmation.delete()
    except discord.HTTPException as e:
        temp_msg = await message.channel.send(f"❌ Failed to move {user.mention}: {str(e)}")
        await asyncio.sleep(5)
        await temp_msg.delete()
        try:
            await reaction.remove(user)
        except discord.HTTPException:
            pass

@bot.event
async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    message = reaction.message
    if message.id not in matches_by_msg:
        return

    match = matches_by_msg[message.id]
    emoji = str(reaction.emoji)
    if emoji not in LANE_REACTIONS:
        return

    if user.id in match['participants']:
        participant_data = match['participants'][user.id]
        if participant_data['lane'] == LANE_REACTIONS[emoji]:
            member = participant_data['member']
            original_channel = bot.get_channel(participant_data['original_channel'])
            if member and member.voice and original_channel:
                try:
                    await member.move_to(original_channel)
                except discord.HTTPException:
                    pass
            match['participants'].pop(user.id, None)

# -----------------------------
# Text controls (stop/pause/resume/status)
# -----------------------------
async def stop_match_text(message: discord.Message, id_or_link: str = None):
    """
    If id_or_link is provided, stop that match (by message id or link).
    Else stop the active match for this channel.
    """
    if id_or_link:
        g_id, c_id, m_id = extract_message_id_or_link(id_or_link)
        if not m_id:
            await message.reply("❌ Could not parse match ID or link.")
            return
        if m_id not in matches_by_msg:
            await message.reply("❌ No match found with that message ID.")
            return
        await end_match(m_id, reason="🛑 Match stopped via message control")
        await message.reply("✅ Match stopped.")
        return

    # no id/link: stop active in this channel
    ch_id = message.channel.id
    if ch_id not in active_match_by_channel:
        await message.reply("❌ No active lane assignment in this channel.")
        return
    m_id = active_match_by_channel[ch_id]
    await end_match(m_id, reason="🛑 Match stopped via message control")
    await message.reply("✅ Match stopped.")

async def pause_match_text(message: discord.Message):
    ch_id = message.channel.id
    if ch_id not in active_match_by_channel:
        await message.reply("❌ No active lane assignment in this channel to pause.")
        return
    m_id = active_match_by_channel[ch_id]
    match = matches_by_msg.get(m_id)
    if not match:
        await message.reply("❌ Match data not found.")
        return
    if match['paused_at'] is not None:
        await message.reply("❌ Match is already paused.")
        return
    match['paused_at'] = datetime.now()
    embed = discord.Embed(title="⏸️ Match Paused", color=0xf39c12)
    embed.set_footer(text=f"Paused by {message.author.display_name}")
    embed.timestamp = datetime.now()
    await message.reply(embed=embed)

async def resume_match_text(message: discord.Message):
    ch_id = message.channel.id
    if ch_id not in active_match_by_channel:
        await message.reply("❌ No active lane assignment in this channel to resume.")
        return
    m_id = active_match_by_channel[ch_id]
    match = matches_by_msg.get(m_id)
    if not match:
        await message.reply("❌ Match data not found.")
        return
    if match['paused_at'] is None:
        await message.reply("❌ Match is not paused.")
        return
    pause_duration = (datetime.now() - match['paused_at']).total_seconds()
    match['total_paused_time'] += pause_duration
    match['paused_at'] = None
    embed = discord.Embed(title="▶️ Match Resumed", color=0x2ecc71)
    embed.set_footer(text=f"Resumed by {message.author.display_name}")
    embed.timestamp = datetime.now()
    await message.reply(embed=embed)

async def show_match_status_text(message: discord.Message):
    ch_id = message.channel.id
    if ch_id not in active_match_by_channel:
        await message.reply("❌ No active lane assignment in this channel.")
        return
    m_id = active_match_by_channel[ch_id]
    await show_match_status_generic(message, m_id)

async def show_match_status_generic(message: discord.Message, msg_id: int):
    match = matches_by_msg.get(msg_id)
    if not match:
        await message.reply("❌ Match not found.")
        return

    now = datetime.now()
    start = match['start_time']
    total_paused = match['total_paused_time']

    if match['paused_at'] is not None:
        elapsed = (match['paused_at'] - start).total_seconds() - total_paused
        status_emoji, status_text, color = "⏸️", "PAUSED", 0xf39c12
    else:
        elapsed = (now - start).total_seconds() - total_paused
        status_emoji, status_text, color = "▶️", "RUNNING", 0x3498db

    elapsed = max(0, elapsed)
    remaining = max(0, MATCH_DURATION - elapsed)
    rm, rs = int(remaining // 60), int(remaining % 60)

    embed = discord.Embed(title=f"{status_emoji} Lane Assignment Status", description=f"**Status:** {status_text}", color=color)
    embed.add_field(name="🧾 Match Message ID", value=str(msg_id), inline=True)
    embed.add_field(name="⏱️ Time Remaining", value=f"{rm}:{rs:02d}", inline=True)
    embed.add_field(name="👥 Total Participants", value=len(match['participants']), inline=True)

    lane_info = []
    guild = match['guild']
    for emoji, lane_name in LANE_REACTIONS.items():
        vc = discord.utils.get(guild.voice_channels, name=lane_name)
        if vc:
            members = [m.display_name for m in vc.members]
            count = len(members)
            if count:
                shown = members[:5]
                if count > 5:
                    shown.append(f"... and {count-5} more")
                lane_info.append(f"{emoji} **{lane_name}** ({count})\n└ {', '.join(shown)}")
            else:
                lane_info.append(f"{emoji} **{lane_name}** (0)")
        else:
            lane_info.append(f"{emoji} **{lane_name}** (Channel not found)")

    if lane_info:
        embed.add_field(name="🎯 Current Lane Distribution", value="\n\n".join(lane_info), inline=False)

    if match['paused_at']:
        embed.add_field(name="💡 Controls", value="Type `resume match` to continue or `stop match` to end", inline=False)
    else:
        embed.add_field(name="💡 Controls", value="Type `pause match` to pause or `stop match` to end", inline=False)

    embed.timestamp = now
    await message.reply(embed=embed)

# -----------------------------
# Timer & end logic
# -----------------------------
@tasks.loop(seconds=30)
async def match_timer():
    now = datetime.now()
    to_end = []
    # iterate safely over a snapshot
    for msg_id, match in list(matches_by_msg.items()):
        if match['paused_at'] is not None:
            continue
        elapsed = (now - match['start_time']).total_seconds() - match['total_paused_time']
        if elapsed >= MATCH_DURATION:
            to_end.append(msg_id)
    for msg_id in to_end:
        await end_match(msg_id, "⏰ Time's up!")

async def end_match(msg_id: int, reason: str = "Match ended"):
    match = matches_by_msg.get(msg_id)
    if not match:
        return
    guild = match['guild']
    channel = bot.get_channel(match['channel_id'])
    moved_users = []

    # Move everyone back (best-effort)
    for user_id, pdata in list(match['participants'].items()):
        member = pdata.get('member')
        original_channel = bot.get_channel(pdata.get('original_channel'))
        if not member or not member.guild:
            continue
        if member.voice and original_channel:
            try:
                await member.move_to(original_channel)
                moved_users.append(member.display_name)
            except discord.HTTPException:
                pass

    # Notify & try to delete the lane message to clean up
    if channel:
        embed = discord.Embed(
            title="🏁 Lane Assignment Complete!",
            description=f"{reason}\n\nAll participants have been moved back to their original voice channels.",
            color=0x2ecc71
        )
        if moved_users:
            embed.add_field(name="Participants Returned", value="\n".join(moved_users[:10]), inline=False)
        embed.timestamp = datetime.now()
        await channel.send(embed=embed)

        # Best-effort: remove the lane message
        try:
            lane_msg = await channel.fetch_message(msg_id)
            await lane_msg.delete()
        except Exception:
            pass

    # Clear indexes
    matches_by_msg.pop(msg_id, None)
    ch_id = match['channel_id']
    if active_match_by_channel.get(ch_id) == msg_id:
        active_match_by_channel.pop(ch_id, None)

# -----------------------------
# Commands (still available)
# -----------------------------
@bot.command(name='end_match')
@commands.has_permissions(move_members=True)
async def end_match_command(ctx):
    ch_id = ctx.channel.id
    if ch_id not in active_match_by_channel:
        await ctx.send("❌ No active lane assignment found in this channel!")
        return
    m_id = active_match_by_channel[ch_id]
    await end_match(m_id, "🛑 Match ended manually")
    await ctx.send("✅ Lane assignment ended successfully!")

@bot.command(name='match_status')
async def match_status_command(ctx):
    ch_id = ctx.channel.id
    if ch_id not in active_match_by_channel:
        await ctx.send("❌ No active lane assignment in this channel.")
        return
    m_id = active_match_by_channel[ch_id]
    dummy_message = ctx.message
    await show_match_status_generic(dummy_message, m_id)

@bot.command(name='setup_lanes')
@commands.has_permissions(administrator=True)
async def setup_lanes(ctx):
    guild = ctx.guild
    category = discord.utils.get(guild.categories, name="Lane Assignments")
    if not category:
        category = await guild.create_category("Lane Assignments")
    created = []
    for channel_name in LANE_REACTIONS.values():
        existing = discord.utils.get(guild.voice_channels, name=channel_name)
        if not existing:
            await guild.create_voice_channel(channel_name, category=category)
            created.append(channel_name)
    if created:
        await ctx.send(f"✅ Created lane channels: {', '.join(created)}")
    else:
        await ctx.send("✅ All lane channels already exist!")

# -----------------------------
# Error handling
# -----------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"Error: {error}")

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    TOKEN = os.environ['token']
    webserver.keep_alive()
    bot.run(TOKEN)
