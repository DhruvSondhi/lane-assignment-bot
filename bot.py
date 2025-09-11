import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime
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
active_matches = {}  # guild_id: {message_id, participants, start_time, original_channels, paused_at, total_paused_time}

# Match configuration
MATCH_DURATION = 400  # 9 minutes and 45 seconds (585 seconds)


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
    
    # Skip if not in a guild
    if not message.guild:
        return
    
    guild_id = message.guild.id
    content = message.content.lower()
    
    # Check if message is in lane-assignment channel and contains trigger phrases
    if message.channel.name.lower() == 'lane-assignment':
        # Fixed the logic error here - was using 'or' instead of proper checking
        if 'start match lane assignments' in content or 'start laning' in content:
            await start_lane_assignment(message)
        elif 'pause match' in content and guild_id in active_matches:
            await pause_match(message)
        elif 'resume match' in content and guild_id in active_matches:
            await resume_match(message)
        elif 'stop match' in content and guild_id in active_matches:
            await stop_match(message)
        elif 'time remaining' in content or 'match status' in content:
            await show_match_status(message)
    
    # Process other commands
    await bot.process_commands(message)

async def start_lane_assignment(message):
    """Start a new lane assignment session"""
    guild_id = message.guild.id
    
    # Check if there's already an active match
    if guild_id in active_matches:
        await message.reply("❌ There's already an active lane assignment! Type 'stop match' to cancel it first.")
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
    embed.add_field(name="💡 Controls", value="Type `pause match`, `stop match`, or `time remaining` in this channel", inline=False)
    
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
        'guild': message.guild,
        'paused_at': None,  # When the match was paused
        'total_paused_time': 0  # Total seconds the match has been paused
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
        try:
            temp_msg = await reaction.message.channel.send(f"❌ {user.mention}, you must be in a voice channel to join a lane!")
            await asyncio.sleep(5)
            await temp_msg.delete()
        except:
            pass
        try:
            await reaction.remove(user)
        except:
            pass
        return
    
    original_channel = member.voice.channel
    target_lane = LANE_REACTIONS[emoji]
    
    # Find the target voice channel
    target_channel = discord.utils.get(member.guild.voice_channels, name=target_lane)
    
    if not target_channel:
        try:
            temp_msg = await reaction.message.channel.send(f"❌ {target_lane} voice channel not found! Use `!setup_lanes` to create it.")
            await asyncio.sleep(5)
            await temp_msg.delete()
        except:
            pass
        return
    
    # Remove user from other lane reactions if they were already assigned
    if user.id in match_data['participants']:
        # Remove their previous reactions
        for old_emoji, old_lane_name in LANE_REACTIONS.items():
            if old_emoji != emoji:
                try:
                    await reaction.message.remove_reaction(old_emoji, user)
                except:
                    pass
    
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
        try:
            confirmation = await reaction.message.channel.send(
                f"✅ {user.mention} assigned to **{target_lane}**!"
            )
            await asyncio.sleep(3)
            await confirmation.delete()
        except:
            pass
            
    except discord.HTTPException:
        try:
            temp_msg = await reaction.message.channel.send(f"❌ Failed to move {user.mention} - check bot permissions!")
            await asyncio.sleep(5)
            await temp_msg.delete()
        except:
            pass
        try:
            await reaction.remove(user)
        except:
            pass

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
    
    # If user was in this lane, move them back to original channel
    if user.id in match_data['participants']:
        participant_data = match_data['participants'][user.id]
        if participant_data['lane'] == LANE_REACTIONS[emoji]:
            member = participant_data['member']
            original_channel = bot.get_channel(participant_data['original_channel'])
            
            if member.voice and original_channel:
                try:
                    await member.move_to(original_channel)
                    del match_data['participants'][user.id]
                    
                    try:
                        temp_msg = await reaction.message.channel.send(
                            f"↩️ {user.mention} moved back to **{original_channel.name}**"
                        )
                        await asyncio.sleep(3)
                        await temp_msg.delete()
                    except:
                        pass
                        
                except discord.HTTPException:
                    pass

async def stop_match(message):
    """Stop the current match and move everyone back immediately"""
    guild_id = message.guild.id
    
    if guild_id not in active_matches:
        await message.reply("❌ No active lane assignment to stop!")
        return
    
    # End the match with stop reason
    await end_match(guild_id, "🛑 Match stopped manually")
    del active_matches[guild_id]
    
    embed = discord.Embed(
        title="🛑 Match Stopped",
        description="The lane assignment has been stopped and all participants have been moved back to their original channels.",
        color=0xe74c3c
    )
    embed.set_footer(text=f"Stopped by {message.author.display_name}")
    embed.timestamp = datetime.now()
    
    await message.reply(embed=embed)

async def pause_match(message):
    """Pause the current match"""
    guild_id = message.guild.id
    
    if guild_id not in active_matches:
        await message.reply("❌ No active lane assignment to pause!")
        return
    
    match_data = active_matches[guild_id]
    
    if match_data['paused_at'] is not None:
        await message.reply("❌ Match is already paused!")
        return
    
    # Record pause time
    match_data['paused_at'] = datetime.now()
    
    embed = discord.Embed(
        title="⏸️ Match Paused",
        description="The lane assignment has been paused. Type 'resume match' to continue.",
        color=0xf39c12
    )
    embed.set_footer(text=f"Paused by {message.author.display_name}")
    embed.timestamp = datetime.now()
    
    await message.reply(embed=embed)

async def resume_match(message):
    """Resume a paused match"""
    guild_id = message.guild.id
    
    if guild_id not in active_matches:
        await message.reply("❌ No active lane assignment to resume!")
        return
    
    match_data = active_matches[guild_id]
    
    if match_data['paused_at'] is None:
        await message.reply("❌ Match is not paused!")
        return
    
    # Calculate how long the match was paused and add to total
    pause_duration = (datetime.now() - match_data['paused_at']).total_seconds()
    match_data['total_paused_time'] += pause_duration
    match_data['paused_at'] = None
    
    embed = discord.Embed(
        title="▶️ Match Resumed",
        description="The lane assignment has been resumed!",
        color=0x2ecc71
    )
    embed.set_footer(text=f"Resumed by {message.author.display_name}")
    embed.timestamp = datetime.now()
    
    await message.reply(embed=embed)

async def show_match_status(message):
    """Show current match status with time and participant info"""
    guild_id = message.guild.id
    
    if guild_id not in active_matches:
        await message.reply("❌ No active lane assignment!")
        return
    
    match_data = active_matches[guild_id]
    current_time = datetime.now()
    start_time = match_data['start_time']
    total_paused_time = match_data['total_paused_time']
    
    # Calculate elapsed time
    if match_data['paused_at'] is not None:
        # Match is paused - don't include current pause time in elapsed
        elapsed = (match_data['paused_at'] - start_time).total_seconds() - total_paused_time
        status_emoji = "⏸️"
        status_text = "PAUSED"
    else:
        # Match is running
        elapsed = (current_time - start_time).total_seconds() - total_paused_time
        status_emoji = "▶️"
        status_text = "RUNNING"
    
    remaining = max(0, MATCH_DURATION - elapsed)
    remaining_minutes = int(remaining // 60)
    remaining_seconds = int(remaining % 60)
    
    embed = discord.Embed(
        title=f"{status_emoji} Lane Assignment Status",
        description=f"**Status:** {status_text}",
        color=0xf39c12 if match_data['paused_at'] else 0x3498db
    )
    
    embed.add_field(
        name="⏱️ Time Remaining", 
        value=f"{remaining_minutes}:{remaining_seconds:02d}", 
        inline=True
    )
    embed.add_field(
        name="👥 Total Participants", 
        value=len(match_data['participants']), 
        inline=True
    )
    
    # Show lane distribution with actual voice channel members
    guild = message.guild
    lane_info = []
    
    for emoji, lane_name in LANE_REACTIONS.items():
        # Get the voice channel
        voice_channel = discord.utils.get(guild.voice_channels, name=lane_name)
        
        if voice_channel:
            # Get members currently in this voice channel
            members_in_vc = [member.display_name for member in voice_channel.members]
            member_count = len(members_in_vc)
            
            if member_count > 0:
                # Limit display to first 5 members to avoid embed limits
                displayed_members = members_in_vc[:5]
                if member_count > 5:
                    displayed_members.append(f"... and {member_count - 5} more")
                
                lane_info.append(f"{emoji} **{lane_name}** ({member_count})\n└ {', '.join(displayed_members)}")
            else:
                lane_info.append(f"{emoji} **{lane_name}** (0)")
        else:
            lane_info.append(f"{emoji} **{lane_name}** (Channel not found)")
    
    if lane_info:
        embed.add_field(name="🎯 Current Lane Distribution", value="\n\n".join(lane_info), inline=False)
    
    # Add pause/resume/stop instructions
    if match_data['paused_at']:
        embed.add_field(name="💡 Controls", value="Type `resume match` to continue or `stop match` to end", inline=False)
    else:
        embed.add_field(name="💡 Controls", value="Type `pause match`, `stop match`, or `time remaining`", inline=False)
    
    embed.timestamp = current_time
    
    await message.reply(embed=embed)

@tasks.loop(seconds=30)  # Check every 30 seconds
async def match_timer():
    """Check if any matches should end"""
    current_time = datetime.now()
    guilds_to_remove = []
    
    for guild_id, match_data in active_matches.items():
        # Skip if match is paused
        if match_data['paused_at'] is not None:
            continue
            
        start_time = match_data['start_time']
        total_paused_time = match_data['total_paused_time']
        elapsed = (current_time - start_time).total_seconds() - total_paused_time
        
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

@bot.command(name='setup_lanes')
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

# Run the bot
if __name__ == "__main__":
    TOKEN = os.environ['token']
    webserver.keep_alive()
    bot.run(TOKEN)



