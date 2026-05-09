import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import re
from collections import deque

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PREFIX = "m!"
TOKEN = os.environ.get("DISCORD_TOKEN")  # Set this in your hosting panel

# ─── YTDL OPTIONS ─────────────────────────────────────────────────────────────
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cookiefile": "cookies.txt",  # optional, helps with age-restricted content
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "opus",
    }],
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -filter:a 'volume=0.5'",
}

# ─── BOT SETUP ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Per-guild queue storage: { guild_id: deque([{ 'url', 'title', 'requester' }]) }
queues: dict[int, deque] = {}
# Track currently playing info per guild
now_playing: dict[int, dict] = {}


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_queue(guild_id: int) -> deque:
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


def is_local_file(query: str) -> bool:
    """Check if the input is a local file path (from an attachment)."""
    return os.path.isfile(query)


def is_url(query: str) -> bool:
    return re.match(r"https?://", query) is not None


async def resolve_source(query: str) -> list[dict]:
    """
    Resolve a query (URL or search term) into a list of track dicts.
    Supports: YouTube, Spotify (via youtube-dl), SoundCloud, direct links, search.
    Returns list of { 'url': stream_url, 'title': str, 'webpage_url': str }
    """
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                # Playlist
                tracks = []
                for entry in info["entries"]:
                    if entry:
                        tracks.append({
                            "url": entry.get("url") or entry.get("webpage_url"),
                            "title": entry.get("title", "Unknown"),
                            "webpage_url": entry.get("webpage_url", query),
                        })
                return tracks
            else:
                return [{
                    "url": info.get("url") or info.get("webpage_url"),
                    "title": info.get("title", "Unknown"),
                    "webpage_url": info.get("webpage_url", query),
                }]

    return await loop.run_in_executor(None, _extract)


async def play_next(ctx: commands.Context):
    """Play the next song in the queue, or disconnect if empty."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    vc: discord.VoiceClient = ctx.voice_client

    if not vc or not vc.is_connected():
        return

    if not queue:
        now_playing.pop(guild_id, None)
        embed = discord.Embed(
            description="✅ Queue finished. Disconnecting...",
            color=0x2b2d31
        )
        await ctx.send(embed=embed)
        await asyncio.sleep(2)
        await vc.disconnect()
        return

    track = queue.popleft()
    now_playing[guild_id] = track

    # If it's a local file (attachment), play directly
    if is_local_file(track["url"]):
        source = discord.FFmpegPCMAudio(track["url"])
    else:
        source = discord.FFmpegOpusAudio(track["url"], **FFMPEG_OPTIONS)

    def after_playing(error):
        if error:
            print(f"[Player error] {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"[After error] {e}")

    vc.play(source, after=after_playing)

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=0x5865f2
    )
    embed.set_footer(text=f"Requested by {track['requester']}")
    await ctx.send(embed=embed)


# ─── EVENTS ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening,
        name="m!p <song>"
    ))


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    embed = discord.Embed(
        description=f"❌ {str(error)}",
        color=0xed4245
    )
    await ctx.send(embed=embed)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Detect when the bot is kicked/disconnected from a voice channel and clean up."""
    if member.id != bot.user.id:
        return

    # Bot was in a channel and is now gone (kicked, disconnected, moved away)
    if before.channel is not None and after.channel is None:
        guild = before.channel.guild
        guild_id = guild.id

        # Clear queue and now playing
        if guild_id in queues:
            queues[guild_id].clear()
        now_playing.pop(guild_id, None)

        # Force stop the voice client if it still exists
        vc = guild.voice_client
        if vc:
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass


# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@bot.command(name="p", aliases=["play"])
async def play(ctx: commands.Context, *, query: str = None):
    """
    m!p <YouTube/Spotify/SoundCloud URL, search term, or attach a file>
    """
    print(f"[DEBUG] m!p triggered by {ctx.author} | query: {query} | attachments: {ctx.message.attachments}")
    # Must be in a voice channel
    if not ctx.author.voice:
        embed = discord.Embed(
            description="❌ You must be in a voice channel to play music.",
            color=0xed4245
        )
        return await ctx.send(embed=embed)

    vc: discord.VoiceClient = ctx.voice_client

    # Join the channel if not already in one
    if not vc:
        vc = await ctx.author.voice.channel.connect()
    elif not vc.is_connected():
        # Stale voice client — reconnect and clear old state
        queues.pop(ctx.guild.id, None)
        now_playing.pop(ctx.guild.id, None)
        vc = await ctx.author.voice.channel.connect()
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)

    queue = get_queue(ctx.guild.id)

    # Handle file attachments (mp3, wav, flac, ogg, etc.)
    if ctx.message.attachments:
        for attachment in ctx.message.attachments:
            if attachment.filename.lower().endswith(
                (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma")
            ):
                save_path = f"/tmp/{attachment.filename}"
                await attachment.save(save_path)
                track = {
                    "url": save_path,
                    "title": attachment.filename,
                    "webpage_url": attachment.url,
                    "requester": str(ctx.author),
                }
                queue.append(track)
                embed = discord.Embed(
                    description=f"📎 Added **{attachment.filename}** to the queue.",
                    color=0x57f287
                )
                await ctx.send(embed=embed)
        if not vc.is_playing() and not vc.is_paused():
            await play_next(ctx)
        return

    if not query:
        embed = discord.Embed(
            description="❌ Please provide a URL or search term.\nExample: `m!p never gonna give you up`",
            color=0xed4245
        )
        return await ctx.send(embed=embed)

    # Show loading indicator
    loading_msg = await ctx.send("🔍 Searching...")

    try:
        tracks = await resolve_source(query)
    except Exception as e:
        await loading_msg.delete()
        embed = discord.Embed(
            description=f"❌ Could not load track: `{e}`",
            color=0xed4245
        )
        return await ctx.send(embed=embed)

    await loading_msg.delete()

    for track in tracks:
        track["requester"] = str(ctx.author)
        queue.append(track)

    if len(tracks) == 1:
        embed = discord.Embed(
            description=f"✅ Added **[{tracks[0]['title']}]({tracks[0]['webpage_url']})** to the queue.",
            color=0x57f287
        )
    else:
        embed = discord.Embed(
            description=f"✅ Added **{len(tracks)} tracks** to the queue.",
            color=0x57f287
        )
    await ctx.send(embed=embed)

    # Start playing if not already
    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx)


@bot.command(name="s", aliases=["skip"])
async def skip(ctx: commands.Context):
    """
    m!s — Skip the currently playing song.
    """
    vc: discord.VoiceClient = ctx.voice_client

    if not vc or not vc.is_playing():
        embed = discord.Embed(
            description="❌ Nothing is currently playing.",
            color=0xed4245
        )
        return await ctx.send(embed=embed)

    vc.stop()  # triggers after_playing → play_next

    embed = discord.Embed(
        description="⏭️ Skipped!",
        color=0xfee75c
    )
    await ctx.send(embed=embed)


@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx: commands.Context):
    """m!queue — Show the current queue."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    current = now_playing.get(guild_id)

    if not current and not queue:
        embed = discord.Embed(
            description="📭 The queue is empty.",
            color=0x2b2d31
        )
        return await ctx.send(embed=embed)

    lines = []
    if current:
        lines.append(f"**Now Playing:** [{current['title']}]({current['webpage_url']})")
    for i, track in enumerate(queue, 1):
        lines.append(f"`{i}.` [{track['title']}]({track['webpage_url']})")

    embed = discord.Embed(
        title="📋 Queue",
        description="\n".join(lines[:20]),  # max 20 shown
        color=0x5865f2
    )
    if len(queue) > 20:
        embed.set_footer(text=f"...and {len(queue) - 20} more")
    await ctx.send(embed=embed)


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    """m!stop — Stop playback and clear the queue."""
    vc: discord.VoiceClient = ctx.voice_client
    if not vc:
        return
    get_queue(ctx.guild.id).clear()
    now_playing.pop(ctx.guild.id, None)
    vc.stop()
    await vc.disconnect()
    embed = discord.Embed(description="⏹️ Stopped and disconnected.", color=0xed4245)
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title="🎵 Music Bot Commands", color=0x5865f2)
    embed.add_field(name="`m!p <url/search>`", value="Play a song or add to queue.\nSupports YouTube, Spotify, SoundCloud, or search terms.", inline=False)
    embed.add_field(name="`m!p` + file attachment", value="Play an uploaded MP3, WAV, FLAC, OGG, etc.", inline=False)
    embed.add_field(name="`m!s`", value="Skip the current song.", inline=False)
    embed.add_field(name="`m!queue`", value="Show the current queue.", inline=False)
    embed.add_field(name="`m!stop`", value="Stop playback and clear the queue.", inline=False)
    await ctx.send(embed=embed)


# ─── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
