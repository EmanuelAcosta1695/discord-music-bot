import asyncio
import logging
import random
import shlex
import sys
import time
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("music-bot")

# ---------------------------------------------------------------------------
# yt-dlp / ffmpeg configuration
# ---------------------------------------------------------------------------

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",  # lets people pass search text instead of a URL
    "source_address": "0.0.0.0",
}

# YouTube's audio URLs are tied to the request headers used to obtain them.
# If ffmpeg fetches the URL with different (or no) headers, YouTube can
# silently serve empty/garbage data instead of a clear error, which sounds
# like "the bot connected but there's no audio". We always send a real
# browser User-Agent, and layer on whatever headers yt-dlp says it used.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)


def format_clock(seconds: int | float | None) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"


def format_duration(seconds):
    if not seconds:
        return "Live/Unknown"
    return format_clock(seconds)


def format_progress(position: int, duration: int | None, width: int = 18) -> str:
    """Return a compact, Discord-friendly scrubber bar."""
    if not duration:
        return f"`{format_clock(position)}`  LIVE"

    ratio = min(max(position / duration, 0), 1)
    filled = min(width, round(ratio * width))
    bar = "▰" * filled + "▱" * (width - filled)
    return f"`{format_clock(position)}`  {bar}  `{format_clock(duration)}`"


class Song:
    __slots__ = (
        "stream_url",
        "title",
        "webpage_url",
        "duration",
        "thumbnail",
        "requester",
        "http_headers",
    )

    def __init__(
        self,
        stream_url,
        title,
        webpage_url,
        duration,
        thumbnail,
        requester=None,
        http_headers=None,
    ):
        self.stream_url = stream_url
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.requester = requester
        self.http_headers = http_headers or {}


async def extract_song(query: str, loop: asyncio.AbstractEventLoop) -> Song:
    """Runs the blocking yt-dlp extraction in a thread pool executor."""

    def _extract():
        return ytdl.extract_info(query, download=False)

    data = await loop.run_in_executor(None, _extract)

    if data is None:
        raise ValueError("No results found.")

    if "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise ValueError("No results found.")
        data = entries[0]

    return Song(
        stream_url=data["url"],
        title=data.get("title", "Unknown title"),
        webpage_url=data.get("webpage_url", query),
        duration=data.get("duration", 0),
        thumbnail=data.get("thumbnail"),
        http_headers=data.get("http_headers") or {},
    )


# ---------------------------------------------------------------------------
# Per-guild state
# ---------------------------------------------------------------------------

class GuildMusicState:
    def __init__(self):
        self.queue: deque[Song] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Song | None = None
        self.loop_mode: str = "off"  # "off" | "song" | "queue"
        self.volume: float = 0.5
        self.text_channel: discord.abc.Messageable | None = None
        self.started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_seconds: float = 0
        self.progress_message: discord.Message | None = None


class MusicControls(discord.ui.View):
    """Buttons attached to the current player card."""

    def __init__(self, music: "Music", guild_id: int):
        super().__init__(timeout=900)
        self.music = music
        self.guild_id = guild_id

    async def get_state(self, interaction: discord.Interaction) -> GuildMusicState | None:
        state = self.music.get_state(self.guild_id)
        user_voice = getattr(interaction.user, "voice", None)
        if (
            state.voice_client is None
            or state.voice_client.channel is None
            or user_voice is None
            or user_voice.channel != state.voice_client.channel
        ):
            await interaction.response.send_message(
                "Join the bot's voice channel to use these controls.", ephemeral=True
            )
            return None
        return state

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self.get_state(interaction)
        if state is None:
            return
        if state.voice_client.is_playing():
            state.paused_at = time.monotonic()
            state.voice_client.pause()
        elif state.voice_client.is_paused():
            if state.paused_at is not None:
                state.paused_seconds += time.monotonic() - state.paused_at
            state.paused_at = None
            state.voice_client.resume()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.music.now_playing_embed(state), view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self.get_state(interaction)
        if state is None:
            return
        if not state.voice_client.is_playing() and not state.voice_client.is_paused():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        state.voice_client.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self.get_state(interaction)
        if state is None:
            return
        state.queue.clear()
        state.current = None
        state.started_at = None
        state.paused_at = None
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect()
            state.voice_client = None
        state.progress_message = None
        stopped = discord.Embed(
            title="Playback stopped",
            description="The queue was cleared and I left the voice channel.",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=stopped, view=None)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}
        self.progress_updater.start()

    def cog_unload(self):
        self.progress_updater.cancel()

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    @staticmethod
    def current_position(state: GuildMusicState) -> int:
        if state.started_at is None:
            return 0
        end_time = state.paused_at if state.paused_at is not None else time.monotonic()
        return max(0, int(end_time - state.started_at - state.paused_seconds))

    def now_playing_embed(self, state: GuildMusicState) -> discord.Embed:
        song = state.current
        assert song is not None

        status = "Paused" if state.paused_at is not None else "Playing"
        embed = discord.Embed(
            title="🎵  NOW PLAYING",
            description=f"### [{song.title}]({song.webpage_url})",
            color=discord.Color.from_rgb(139, 92, 246),
        )
        embed.set_author(name="Discord Music Player")
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(
            name="⏱  Progress",
            value=format_progress(self.current_position(state), song.duration),
            inline=False,
        )
        if song.requester:
            embed.add_field(name="Requested by", value=song.requester.mention, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.set_footer(
            text=f"Volume {int(state.volume * 100)}%  •  Loop: {state.loop_mode.title()}"
        )
        return embed

    @tasks.loop(seconds=10)
    async def progress_updater(self):
        """Refresh the existing card instead of sending a message every tick."""
        for state in self.states.values():
            if (
                state.current is None
                or state.progress_message is None
                or state.voice_client is None
                or not (state.voice_client.is_playing() or state.voice_client.is_paused())
            ):
                continue
            try:
                await state.progress_message.edit(embed=self.now_playing_embed(state))
            except (discord.HTTPException, discord.Forbidden):
                state.progress_message = None

    @progress_updater.before_loop
    async def before_progress_updater(self):
        await self.bot.wait_until_ready()

    # -- Playback engine ---------------------------------------------------

    async def play_next(self, guild: discord.Guild):
        state = self.get_state(guild.id)

        if state.voice_client is None or not state.voice_client.is_connected():
            return

        if state.loop_mode == "song" and state.current is not None:
            next_song = state.current
        elif state.queue:
            next_song = state.queue.popleft()
            if state.loop_mode == "queue" and state.current is not None:
                state.queue.append(state.current)
        else:
            state.current = None
            return

        state.current = next_song
        state.progress_message = None

        headers = {**DEFAULT_HEADERS, **next_song.http_headers}
        header_block = "".join(f"{key}: {value}\r\n" for key, value in headers.items())

        # discord.py parses these values with shlex, so they must be strings;
        # passing a list causes the library to ignore the options entirely.
        before_options = (
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            f"-headers {shlex.quote(header_block)}"
        )

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    next_song.stream_url,
                    before_options=before_options,
                    options="-vn",
                    # Keep the real HTTP/decoder failure in the console.
                    stderr=sys.stderr,
                ),
                volume=state.volume,
            )
        except Exception as e:
            log.error(f"Failed to create audio source: {e}")
            if state.text_channel:
                await state.text_channel.send(
                    f"Couldn't play **{next_song.title}**, skipping it. ({e})"
                )
            await self.play_next(guild)
            return

        def after_playing(error):
            if error:
                log.error(f"Player error: {error}")
            fut = asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)
            try:
                fut.result()
            except Exception as e:
                log.error(f"Error advancing queue: {e}")

        state.voice_client.play(source, after=after_playing)
        state.started_at = time.monotonic()
        state.paused_at = None
        state.paused_seconds = 0

        if state.text_channel:
            state.progress_message = await state.text_channel.send(
                embed=self.now_playing_embed(state),
                view=MusicControls(self, guild.id),
            )

    # -- Auto-leave when alone ----------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            return

        state = self.states.get(member.guild.id)
        if not state or not state.voice_client:
            return

        channel = state.voice_client.channel
        if channel is None:
            return

        non_bot_members = [m for m in channel.members if not m.bot]
        if non_bot_members:
            return

        await asyncio.sleep(30)

        # Re-check in case someone rejoined during the wait
        non_bot_members = [m for m in channel.members if not m.bot]
        if not non_bot_members and state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect()
            state.voice_client = None
            state.queue.clear()
            state.current = None
            if state.text_channel:
                await state.text_channel.send(
                    "Left the voice channel since everyone left. 👋"
                )

    # -- Commands ------------------------------------------------------------

    @app_commands.command(name="join", description="Join your current voice channel")
    async def join(self, interaction: discord.Interaction):
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        channel = interaction.user.voice.channel
        state = self.get_state(interaction.guild.id)
        state.text_channel = interaction.channel

        if state.voice_client is None or not state.voice_client.is_connected():
            state.voice_client = await channel.connect()
        else:
            await state.voice_client.move_to(channel)

        await interaction.response.send_message(f"Joined **{channel.name}** 🔊")

    @app_commands.command(name="play", description="Play a YouTube link or search, or queue it up")
    @app_commands.describe(query="YouTube URL, or search terms (e.g. 'daft punk one more time')")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send("You need to be in a voice channel first.")
            return

        voice_channel = interaction.user.voice.channel
        state = self.get_state(interaction.guild.id)
        state.text_channel = interaction.channel

        if state.voice_client is None or not state.voice_client.is_connected():
            state.voice_client = await voice_channel.connect()
        elif state.voice_client.channel != voice_channel:
            await state.voice_client.move_to(voice_channel)

        try:
            song = await extract_song(query, self.bot.loop)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load that: `{e}`")
            return

        song.requester = interaction.user
        state.queue.append(song)

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            embed = discord.Embed(
                title="Added to Queue",
                description=f"[{song.title}]({song.webpage_url})",
                color=discord.Color.green(),
            )
            embed.add_field(name="Position in queue", value=str(len(state.queue)))
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Loading **{song.title}**...")
            await self.play_next(interaction.guild)

    @app_commands.command(name="skip", description="Skip the current song (alias: next)")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if not state.voice_client or not (
            state.voice_client.is_playing() or state.voice_client.is_paused()
        ):
            await interaction.response.send_message("Nothing is playing right now.")
            return
        state.voice_client.stop()  # triggers the after_playing callback -> play_next
        await interaction.response.send_message("Skipped ⏭️")

    # /next is just a friendly alias for /skip, since that's what was requested
    @app_commands.command(name="next", description="Skip to the next song in the queue")
    async def next_(self, interaction: discord.Interaction):
        await self.skip.callback(self, interaction)

    @app_commands.command(name="stop", description="Stop playback, clear the queue, and leave the voice channel")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        state.queue.clear()
        state.current = None
        state.loop_mode = "off"
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect()
            state.voice_client = None
        await interaction.response.send_message("Stopped playback and cleared the queue. 🛑")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.voice_client and state.voice_client.is_playing():
            state.paused_at = time.monotonic()
            state.voice_client.pause()
            await interaction.response.send_message("Paused ⏸️")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.voice_client and state.voice_client.is_paused():
            if state.paused_at is not None:
                state.paused_seconds += time.monotonic() - state.paused_at
            state.paused_at = None
            state.voice_client.resume()
            await interaction.response.send_message("Resumed ▶️")
        else:
            await interaction.response.send_message("Nothing is paused.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if not state.current and not state.queue:
            await interaction.response.send_message("The queue is empty.")
            return

        lines = []
        if state.current:
            lines.append(f"**Now Playing:** [{state.current.title}]({state.current.webpage_url})")
        for i, song in enumerate(list(state.queue)[:10], start=1):
            requester = song.requester.mention if song.requester else "Unknown"
            lines.append(f"`{i}.` [{song.title}]({song.webpage_url}) — {requester}")
        if len(state.queue) > 10:
            lines.append(f"...and {len(state.queue) - 10} more")

        embed = discord.Embed(
            title="🎵 Music Queue", description="\n".join(lines), color=discord.Color.blurple()
        )
        embed.set_footer(text=f"Loop mode: {state.loop_mode}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if not state.current:
            await interaction.response.send_message("Nothing is playing right now.")
            return
        song = state.current
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.blurple(),
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Duration", value=format_duration(song.duration))
        if song.requester:
            embed.add_field(name="Requested by", value=song.requester.mention)
        embed = self.now_playing_embed(state)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Remove a song from the queue by its position")
    @app_commands.describe(position="Position in the queue (see /queue)")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild.id)
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message("Invalid position.", ephemeral=True)
            return
        removed = state.queue[position - 1]
        del state.queue[position - 1]
        await interaction.response.send_message(f"Removed **{removed.title}** from the queue.")

    @app_commands.command(name="clear", description="Clear the queue (keeps the current song playing)")
    async def clear(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        state.queue.clear()
        await interaction.response.send_message("Queue cleared. 🧹")

    @app_commands.command(name="shuffle", description="Shuffle the songs currently in the queue")
    async def shuffle(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if len(state.queue) < 2:
            await interaction.response.send_message("Not enough songs in the queue to shuffle.")
            return
        random.shuffle(state.queue)
        await interaction.response.send_message("Queue shuffled 🔀")

    @app_commands.command(name="loop", description="Set the loop mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Song (repeat current song)", value="song"),
            app_commands.Choice(name="Queue (repeat whole queue)", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = self.get_state(interaction.guild.id)
        state.loop_mode = mode.value
        await interaction.response.send_message(f"Loop mode set to **{mode.name}**.")

    @app_commands.command(name="volume", description="Set playback volume (0-100)")
    @app_commands.describe(level="Volume percentage, 0 to 100")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        state = self.get_state(interaction.guild.id)
        state.volume = level / 100
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        await interaction.response.send_message(f"Volume set to {level}%. 🔊")

    @app_commands.command(name="leave", description="Leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None
            state.queue.clear()
            state.current = None
        await interaction.response.send_message("Left the voice channel. 👋")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
