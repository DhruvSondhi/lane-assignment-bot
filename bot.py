import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta
import json
import os
import webserver

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Data storage for active matches
active_matches = {}  # guild_id: {message_id, participants, start_time, original_channels}

# Match configuration
MATCH_DURATION = 585  # 9 minutes and 45 seconds (585 seconds)

LANE_REACTIONS = {
    '🟡': 'Lane - Yellow',
    '🔵': 'Lane - Blue', 
    '🟢': 'Lane - Green'
}

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    print('Lane Assignment Bot is ready!')
    
    # Start the match timer task
    match_timer.start()

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return
    
    # Check if message is in general chat and contains trigger phrase
    if (message.channel.name.lower() in ['lane-assignment', 'bot-spam'] and 
        'start match lane assignments' in message.content.lower()):
        
        await start_lane_assignment(message)
    
    # Process other commands
    await bot.process_commands(message)

async def start_lane_assignment(message):
    """Start a new lane assignment session"""
    guild_id = message.guild.id
    
    # Check if there's already an active match
    if guild_id in active_matches:
        await message.reply("❌ There's already an active lane assignment! Wait for it to finish or use `!end_match` to cancel it.")
        return
    
    # Create the lane selection embed
    embed = discord.Embed(
        title="🎯 Lane Assignments Started!",
        description=f"React with your preferred lane. You'll be moved automatically!\n\n**Match Duration:** 9 minutes 45 seconds",
        color=0xe74c3c
    )
    
    embed.add_field(name="🟡", value="Lane - Yellow", inline=True)
    embed.add_field(name="🔵", value="Lane - Blue", inline=True)
    embed.add_field(name="🟢", value="Lane - Green", inline=True)
    
    embed.add_field(name="⚠️ Important", value="You must be in a voice channel to be moved!", inline=False)
    
    embed.set_footer(text=f"Started by {message.author.display_name}")
    embed.timestamp = datetime.now()
    
    # Send the embed message
    lane_message = await message.channel.send(embed=embed)
    
    # Add reactions
    for emoji in LANE_REACTIONS.keys():
        await lane_message.add_reaction(emoji)
    
    # Initialize match data
    active_matches[guild_id] = {
        'message_id': lane_message.id,
        'channel_id': message.channel.id,
        'participants': {},  # user_id: {'lane': lane_name, 'original_channel': channel_id}
        'start_time': datetime.now(),
        'guild': message.guild
    }
    
    await message.reply(f"✅ Lane assignment started! Match will last **9 minutes 45 seconds**.")

@bot.event
async def on_reaction_add(reaction, user):
    """Handle lane selection reactions"""
    if user.bot:
        return
    
    guild_id = reaction.message.guild.id
    
    # Check if this is an active lane assignment message
    if guild_id not in active_matches:
        return
    
    match_data = active_matches[guild_id]
    if reaction.message.id != match_data['message_id']:
        return
    
    # Check if the reaction is a valid lane selection
    emoji = str(reaction.emoji)
    if emoji not in LANE_REACTIONS:
        return
    
    # Get the member and check if they're in a voice channel
    member = reaction.message.guild.get_member(user.id)
    if not member or not member.voice or not member.voice.channel:
        # Send a temporary message
        temp_msg = await reaction.message.channel.send(f"❌ {user.mention}, you must be in a voice channel to join a lane!")
        await asyncio.sleep(5)
        await temp_msg.delete()
        await reaction.remove(user)
        return
    
    original_channel = member.voice.channel
    target_lane = LANE_REACTIONS[emoji]
    
    # Find the target voice channel
    target_channel = discord.utils.get(member.guild.voice_channels, name=target_lane)
    
    if not target_channel:
        temp_msg = await reaction.message.channel.send(f"❌ {target_lane} voice channel not found! Please create it first.")
        await asyncio.sleep(5)
        await temp_msg.delete()
        return
    
    # Remove user from other lane reactions if they were already assigned
    if user.id in match_data['participants']:
        old_lane = match_data['participants'][user.id]['lane']
        # Remove their previous reactions
        for old_emoji, old_lane_name in LANE_REACTIONS.items():
            if old_lane_name == old_lane and old_emoji != emoji:
                await reaction.message.remove_reaction(old_emoji, user)
    
    try:
        # Move the user
        await member.move_to(target_channel)
        
        # Update participant data
        match_data['participants'][user.id] = {
            'lane': target_lane,
            'original_channel': original_channel.id,
            'member': member
        }
        
        # Confirmation message
        confirmation = await reaction.message.channel.send(
            f"✅ {user.mention} has been assigned to **{target_lane}**!"
        )
        await asyncio.sleep(3)
        await confirmation.delete()
        
    except discord.HTTPException as e:
        temp_msg = await reaction.message.channel.send(f"❌ Failed to move {user.mention}: {str(e)}")
        await asyncio.sleep(5)
        await temp_msg.delete()
        await reaction.remove(user)

@bot.event
async def on_reaction_remove(reaction, user):
    """Handle when someone removes their lane reaction"""
    if user.bot:
        return
    
    guild_id = reaction.message.guild.id
    
    if guild_id not in active_matches:
        return
    
    match_data = active_matches[guild_id]
    if reaction.message.id != match_data['message_id']:
        return
    
    emoji = str(reaction.emoji)
    if emoji not in LANE_REACTIONS:
        return
    
    # If user was in this lane, move them back to general voice chat
    if user.id in match_data['participants']:
        participant_data = match_data['participants'][user.id]
        if participant_data['lane'] == LANE_REACTIONS[emoji]:
            member = participant_data['member']
            original_channel = bot.get_channel(participant_data['original_channel'])
            
            if member.voice and original_channel:
                try:
                    await member.move_to(original_channel)
                    del match_data['participants'][user.id]
                    
                    temp_msg = await reaction.message.channel.send(
                        f"↩️ {user.mention} has been moved back to **{original_channel.name}**"
                    )
                    await asyncio.sleep(3)
                    await temp_msg.delete()
                    
                except discord.HTTPException:
                    pass

@tasks.loop(seconds=30)  # Check every 30 seconds
async def match_timer():
    """Check if any matches should end"""
    current_time = datetime.now()
    guilds_to_remove = []
    
    for guild_id, match_data in active_matches.items():
        start_time = match_data['start_time']
        elapsed = (current_time - start_time).total_seconds()
        
        # Check if match time is up
        if elapsed >= MATCH_DURATION:
            await end_match(guild_id, "⏰ Time's up!")
            guilds_to_remove.append(guild_id)
    
    # Remove completed matches
    for guild_id in guilds_to_remove:
        del active_matches[guild_id]

async def end_match(guild_id, reason="Match ended"):
    """End an active match and move everyone back"""
    if guild_id not in active_matches:
        return
    
    match_data = active_matches[guild_id]
    guild = match_data['guild']
    channel = bot.get_channel(match_data['channel_id'])
    
    moved_users = []
    
    # Move all participants back to their original channels
    for user_id, participant_data in match_data['participants'].items():
        member = participant_data['member']
        original_channel = bot.get_channel(participant_data['original_channel'])
        
        if member.voice and original_channel:
            try:
                await member.move_to(original_channel)
                moved_users.append(member.display_name)
            except discord.HTTPException:
                pass
    
    # Send completion message
    if channel:
        embed = discord.Embed(
            title="🏁 Lane Assignment Complete!",
            description=f"{reason}\n\nAll participants have been moved back to their original voice channels.",
            color=0x2ecc71
        )
        
        if moved_users:
            embed.add_field(
                name="Participants Returned",
                value="\n".join(moved_users[:10]),  # Limit to 10 names to avoid embed limits
                inline=False
            )
        
        embed.timestamp = datetime.now()
        await channel.send(embed=embed)

@bot.command(name='end_match')
@commands.has_permissions(move_members=True)
async def end_match_command(ctx):
    """Manually end the current lane assignment"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_matches:
        await ctx.send("❌ No active lane assignment found!")
        return
    
    await end_match(guild_id, "🛑 Match ended manually")
    del active_matches[guild_id]
    await ctx.send("✅ Lane assignment ended successfully!")

@bot.command(name='match_status')
async def match_status(ctx):
    """Check the status of the current match"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_matches:
        await ctx.send("❌ No active lane assignment!")
        return
    
    match_data = active_matches[guild_id]
    start_time = match_data['start_time']
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining = max(0, MATCH_DURATION - elapsed)
    
    embed = discord.Embed(
        title="📊 Current Match Status",
        color=0x3498db
    )
    
    embed.add_field(name="⏱️ Time Remaining", value=f"{int(remaining//60)}:{int(remaining%60):02d}", inline=True)
    embed.add_field(name="👥 Participants", value=len(match_data['participants']), inline=True)
    
    # Show lane distribution
    lane_counts = {}
    for participant in match_data['participants'].values():
        lane = participant['lane']
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
    
    if lane_counts:
        lane_info = []
        for lane, count in lane_counts.items():
            emoji = next(k for k, v in LANE_REACTIONS.items() if v == lane)
            lane_info.append(f"{emoji} {lane}: {count}")
        
        embed.add_field(name="Lane Distribution", value="\n".join(lane_info), inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='setup_lanes')
@commands.has_permissions(administrator=True)
async def setup_lanes(ctx):
    """Create the necessary voice channels for lane assignments"""
    guild = ctx.guild
    category = discord.utils.get(guild.categories, name="Lane Assignments")
    
    if not category:
        category = await guild.create_category("Lane Assignments")
    
    channels_to_create = list(LANE_REACTIONS.values())
    created_channels = []
    
    for channel_name in channels_to_create:
        existing = discord.utils.get(guild.voice_channels, name=channel_name)
        if not existing:
            channel = await guild.create_voice_channel(channel_name, category=category)
            created_channels.append(channel_name)
    
    if created_channels:
        await ctx.send(f"✅ Created lane channels: {', '.join(created_channels)}")
    else:
        await ctx.send("✅ All lane channels already exist!")

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
    else:
        print(f"Error: {error}")

# Run the bot
if __name__ == "__main__":
    # Replace with your bot token
    TOKEN = os.environ['token']
    webserver.keep_alive()
    bot.run(TOKEN)