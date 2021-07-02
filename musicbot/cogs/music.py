from discord.ext import commands
import discord
import asyncio
import youtube_dl
import logging
import math
import random
import heapq
from urllib import request
from ..video import Video
from ..video import Setlist

# TODO: abstract FFMPEG options into their own file?
FFMPEG_BEFORE_OPTS = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
"""
Command line options to pass to `ffmpeg` before the `-i`.

See https://stackoverflow.com/questions/43218292/youtubedl-read-error-with-discord-py/44490434#44490434 for more information.
Also, https://ffmpeg.org/ffmpeg-protocols.html for command line option reference.
"""


async def audio_playing(ctx):
    """Checks that audio is currently playing before continuing."""
    client = ctx.guild.voice_client
    if client and client.channel and client.source:
        return True
    else:
        raise commands.CommandError("Not currently playing any audio.")


async def in_voice_channel(ctx):
    """Checks that the command sender is in the same voice channel as the bot."""
    voice = ctx.author.voice
    bot_voice = ctx.guild.voice_client
    if voice and bot_voice and voice.channel and bot_voice.channel and voice.channel == bot_voice.channel:
        return True
    else:
        raise commands.CommandError(
            "You need to be in the channel to do that.")


async def is_audio_requester(ctx):
    """Checks that the command sender is the song requester."""
    music = ctx.bot.get_cog("Music")
    state = music.get_state(ctx.guild)
    permissions = ctx.channel.permissions_for(ctx.author)
    if permissions.administrator or state.is_requester(ctx.author):
        return True
    else:
        raise commands.CommandError(
            "You need to be the song requester to do that.")


class Music(commands.Cog):
    """Bot commands to help play music."""

    def __init__(self, bot, config):
        self.bot = bot
        self.config = config[__name__.split(".")[
            -1]]  # retrieve module name, find config entry
        self.states = {}
        self.bot.add_listener(self.on_reaction_add, "on_reaction_add")

    def get_state(self, guild):
        """Gets the state for `guild`, creating it if it does not exist."""
        if guild.id in self.states:
            return self.states[guild.id]
        else:
            self.states[guild.id] = GuildState()
            return self.states[guild.id]

    @commands.command(aliases=["stop"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def leave(self, ctx):
        """Leaves the voice channel, if currently in one."""
        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)
        if client and client.channel:
            await client.disconnect()
            state.playlist = []
            state.now_playing = None
        else:
            raise commands.CommandError("Not in a voice channel.")

    @commands.command(aliases=["resume", "p"])
    @commands.guild_only()
    @commands.check(audio_playing)
    @commands.check(in_voice_channel)
    @commands.check(is_audio_requester)
    async def pause(self, ctx):
        """Pauses any currently playing audio."""
        client = ctx.guild.voice_client
        self._pause_audio(client)

    def _pause_audio(self, client):
        if client.is_paused():
            client.resume()
        else:
            client.pause()

    @commands.command(aliases=["vol", "v"])
    @commands.guild_only()
    @commands.check(audio_playing)
    @commands.check(in_voice_channel)
    @commands.check(is_audio_requester)
    async def volume(self, ctx, volume: int):
        """Change the volume of currently playing audio (values 0-250)."""
        state = self.get_state(ctx.guild)

        # make sure volume is nonnegative
        if volume < 0:
            volume = 0

        max_vol = self.config["max_volume"]
        if max_vol > -1:  # check if max volume is set
            # clamp volume to [0, max_vol]
            if volume > max_vol:
                volume = max_vol

        client = ctx.guild.voice_client

        state.volume = float(volume) / 100.0
        client.source.volume = state.volume  # update the AudioSource's volume to match

    @commands.command()
    @commands.guild_only()
    @commands.check(audio_playing)
    @commands.check(in_voice_channel)
    async def skip(self, ctx):
        """Skips the currently playing song, or votes to skip it."""
        state = self.get_state(ctx.guild)
        client = ctx.guild.voice_client
        if ctx.channel.permissions_for(
                ctx.author).administrator or state.is_requester(ctx.author):
            # immediately skip if requester or admin
            client.stop()
        elif self.config["vote_skip"]:
            # vote to skip song
            channel = client.channel
            self._vote_skip(channel, ctx.author)
            # announce vote
            users_in_channel = len([
                member for member in channel.members if not member.bot
            ])  # don't count bots
            required_votes = math.ceil(
                self.config["vote_skip_ratio"] * users_in_channel)
            await ctx.send(
                f"{ctx.author.mention} voted to skip ({len(state.skip_votes)}/{required_votes} votes)"
            )
        else:
            raise commands.CommandError("Sorry, vote skipping is disabled.")

    def _vote_skip(self, channel, member):
        """Register a vote for `member` to skip the song playing."""
        logging.info(f"{member.name} votes to skip")
        state = self.get_state(channel.guild)
        state.skip_votes.add(member)
        users_in_channel = len([
            member for member in channel.members if not member.bot
        ])  # don't count bots
        if (float(len(state.skip_votes)) /
                users_in_channel) >= self.config["vote_skip_ratio"]:
            # enough members have voted to skip, so skip the song
            logging.info(f"Enough votes, skipping...")
            channel.guild.voice_client.stop()

    async def _set_status(self, song=None):
        if song:
            await self.bot.change_presence(activity=discord.Game(name=f"♫ {song.title}"))
        else:
            await self.bot.change_presence(activity=None)


    def _play_song(self, client, state, song):
        state.now_playing = song
        state.skip_votes = set()  # clear skip votes
        asyncio.run_coroutine_threadsafe(self._set_status(song=song), self.bot.loop)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(song.stream_url, before_options=FFMPEG_BEFORE_OPTS), volume=state.volume)

        def after_playing(err):
            if len(state.playlist) > 0:
                next_song = state.playlist.pop(0)
                self._play_song(client, state, next_song)
            else:
                asyncio.run_coroutine_threadsafe(client.disconnect(),
                                                 self.bot.loop)
                asyncio.run_coroutine_threadsafe(self._set_status(), self.bot.loop)

        client.play(source, after=after_playing)

    @commands.command(aliases=["np"])
    @commands.guild_only()
    @commands.check(audio_playing)
    async def nowplaying(self, ctx):
        """Displays information about the current song."""
        state = self.get_state(ctx.guild)
        message = await ctx.send("", embed=state.now_playing.get_embed())
        await self._add_reaction_controls(message)

    @commands.command(aliases=["q", "playlist"])
    @commands.guild_only()
    @commands.check(audio_playing)
    async def queue(self, ctx):
        """Display the current play queue."""
        state = self.get_state(ctx.guild)
        await ctx.send(self._queue_text(state.playlist))

    def _queue_text(self, queue):
        """Returns a block of text describing a given song queue."""
        if len(queue) > 0:
            message = [f"{len(queue)} songs in queue:"]
            message += [
                f"  {index+1}. **{song.title}** (requested by **{song.requested_by.display_name}**)"
                for (index, song) in enumerate(queue)
            ]  # add individual songs
            return "\n".join(message)
        else:
            return "The play queue is empty."

    @commands.command(aliases=["cq"])
    @commands.guild_only()
    @commands.check(audio_playing)
    @commands.has_permissions(administrator=True)
    async def clearqueue(self, ctx):
        """Clears the play queue without leaving the channel."""
        state = self.get_state(ctx.guild)
        state.playlist = []

    @commands.command(aliases=["jq"])
    @commands.guild_only()
    @commands.check(audio_playing)
    @commands.has_permissions(administrator=True)
    async def jumpqueue(self, ctx, song: int, new_index: int):
        """Moves song at an index to `new_index` in queue."""
        state = self.get_state(ctx.guild)  # get state for this guild
        if 1 <= song <= len(state.playlist) and 1 <= new_index:
            song = state.playlist.pop(song - 1)  # take song at index...
            state.playlist.insert(new_index - 1, song)  # and insert it.

            await ctx.send(self._queue_text(state.playlist))
        else:
            raise commands.CommandError("You must use a valid index.")

    @commands.command(brief="Plays audio from <url>.")
    @commands.guild_only()
    async def play(self, ctx, *, url):
        """Plays audio hosted at <url> (or performs a search for <url> and plays the first result)."""

        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)  # get the guild's state

        await self._play(ctx, client, state, url)

    async def _play(self, ctx, client, state, url):
        if client and client.channel:
            try:
                video = Video(url, ctx.author)
            except youtube_dl.DownloadError as e:
                logging.warn(f"Error downloading video: {e}")
                await ctx.send(
                    "There was an error downloading your video, sorry.")
                return
            state.playlist.append(video)
            message = await ctx.send(
                "Added to queue.", embed=video.get_embed())
            await self._add_reaction_controls(message)
        else:
            if ctx.author.voice is not None and ctx.author.voice.channel is not None:
                channel = ctx.author.voice.channel
                try:
                    video = Video(url, ctx.author)
                except youtube_dl.DownloadError as e:
                    await ctx.send(
                        "There was an error downloading your video, sorry.")
                    return
                client = await channel.connect()
                self._play_song(client, state, video)
                message = await ctx.send("", embed=video.get_embed())
                await self._add_reaction_controls(message)
                logging.info(f"Now playing '{video.title}'")
            else:
                raise commands.CommandError(
                    "You need to be in a voice channel to do that.")


    @commands.command(brief="Queue <url> to play after the one currently playing")
    @commands.guild_only()
    async def playnext(self, ctx, *, url):
        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)

        # TODO: maybe make better "nowplaying" checking logic
        if not client:
            await self._play(ctx, client, state, url)
        else:
            try:
                video = Video(url, ctx.author)
            except youtube_dl.DownloadError as e:
                logging.warn(f"Error downloading video: {e}")
                await ctx.send(
                    "There was an error downloading your video, sorry.")
                return

            state.playlist.insert(0, video)

    # TODO: probably make this admin-only, vote, etc
    @commands.command(brief="Stop the current song and play <url> right now")
    @commands.guild_only()
    async def playnow(self, ctx, *, url):
        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)

        if not client:
            await self._play(ctx, client, state, url)
        else:
            try:
                video = Video(url, ctx.author)
            except youtube_dl.DownloadError as e:
                logging.warn(f"Error downloading video: {e}")
                await ctx.send(
                    "There was an error downloading your video, sorry.")
                return

            state.playlist.insert(0, video)
            client.stop()


    @commands.command(brief="Register the playlist at <url> to the requesting user")
    @commands.guild_only()
    async def setlist(self, ctx, *, url):
        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)  # get the guild's state

        if url == "remove":
            del state.setlists[ctx.author]
            await ctx.send(f"Deleted playlist for {ctx.author.display_name}")
            return

        state.setlists[ctx.author] = Setlist(url, ctx.author)
        
        await ctx.send(f"Playlist registered for {ctx.author.display_name}")

        #self._shuffle_setlists(state, client)
        #await self._play(ctx, client, state, state.playlist.pop(0).video_url)

    # Shuffle all user's setlists together
    def _shuffle_setlists(self, state, client):
        temp = []
        # Grab a random 5 songs from each user's setlists
        for user,setlist in state.setlists.items():
            temp += list(map(lambda x: Video(x, user), random.sample(setlist, k=5)))

        # Shuffle all the songs together
        random.shuffle(temp)
        state.playlist = temp

    @commands.command(brief="TODO")
    @commands.guild_only()
    async def build(self, ctx, *, num):
        try:
            num = int(num)
            if num <= 0:
                raise Exception("not greater than zero")
        except:
            await ctx.send(f"{num} is not an integer greater than zero")

        state = self.get_state(ctx.guild)
        if not state.setlists.items():
            await ctx.send("No registered setlists, ignoring")
            return

        client = ctx.guild.voice_client

        state.playlist_state = PlaylistState(state.setlists)
        state.playlist = state.playlist_state.get_num(num)

        await self._play(ctx, client, state, state.playlist.pop(0).video_url)



    @commands.command(brief="Reshuffle user setlists and generate a new queue")
    @commands.guild_only()
    @commands.check(audio_playing)
    async def reshuffle(self, ctx):
        client = ctx.guild.voice_client
        state = self.get_state(ctx.guild)  # get the guild's state

        await ctx.send("Regenerating play queue.")
        self._shuffle_setlists(state, client)
        await ctx.send(self._queue_text(state.playlist))

 
    async def on_reaction_add(self, reaction, user):
        """Respods to reactions added to the bot's messages, allowing reactions to control playback."""
        message = reaction.message
        if user != self.bot.user and message.author == self.bot.user:
            await message.remove_reaction(reaction, user)
            if message.guild and message.guild.voice_client:
                user_in_channel = user.voice and user.voice.channel and user.voice.channel == message.guild.voice_client.channel
                permissions = message.channel.permissions_for(user)
                guild = message.guild
                state = self.get_state(guild)
                if permissions.administrator or (
                        user_in_channel and state.is_requester(user)):
                    client = message.guild.voice_client
                    if reaction.emoji == "⏯":
                        # pause audio
                        self._pause_audio(client)
                    elif reaction.emoji == "⏭":
                        # skip audio
                        client.stop()
                    elif reaction.emoji == "⏮":
                        state.playlist.insert(
                            0, state.now_playing
                        )  # insert current song at beginning of playlist
                        client.stop()  # skip ahead
                elif reaction.emoji == "⏭" and self.config["vote_skip"] and user_in_channel and message.guild.voice_client and message.guild.voice_client.channel:
                    # ensure that skip was pressed, that vote skipping is
                    # enabled, the user is in the channel, and that the bot is
                    # in a voice channel
                    voice_channel = message.guild.voice_client.channel
                    self._vote_skip(voice_channel, user)
                    # announce vote
                    channel = message.channel
                    users_in_channel = len([
                        member for member in voice_channel.members
                        if not member.bot
                    ])  # don't count bots
                    required_votes = math.ceil(
                        self.config["vote_skip_ratio"] * users_in_channel)
                    await channel.send(
                        f"{user.mention} voted to skip ({len(state.skip_votes)}/{required_votes} votes)"
                    )

    async def _add_reaction_controls(self, message):
        """Adds a 'control-panel' of reactions to a message that can be used to control the bot."""
        CONTROLS = ["⏮", "⏯", "⏭"]
        for control in CONTROLS:
            await message.add_reaction(control)

    # TODO: Holy crap absolutely don't expose this one to the public.
    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def debug(self, ctx, *, url):
        state = self.get_state(ctx.guild)  # get the guild's state

        try:
            ret = f"```{str(eval(url))[:1900]}```"
        except Exception as e:
            ret = e

        await ctx.send(f"{ret}")

class GuildState:
    """Helper class managing per-guild state."""

    def __init__(self):
        self.volume = 1.0
        self.playlist = []
        self.skip_votes = set()
        self.now_playing = None

        # userid -> Setlist
        self.setlists = {}
        self.playlist_state = None

    def is_requester(self, user):
        return self.now_playing.requested_by == user


class PlaylistState:
    """Helper class to manage a playlist state"""

    # users: list(userid, userid...)
    def __init__(self, setlists):
        # list((num, userid))
        self.user_playtime = [(0, u) for u in setlists.keys()]
        random.shuffle(self.user_playtime) # ensure the first song picked is random

        # userid -> Setlist
        # copy from guild state, pops played songs
        self.user_setlists = {u:v.copy() for u,v in setlists.items()}

        # Shuffle each setlist so we can always just take from the front
        for _,v in self.user_setlists.items():
            random.shuffle(v)

    # Get a list of <num> songs, increment play times
    def get_num(self, num):
        ret = []

        for i in range(num):
            time, userid = heapq.heappop(self.user_playtime)

            # TODO: refill playlist when a user's runs out
            video = self.user_setlists[userid].pop(0)
            video = Video(video, userid)

            ret.append(video)
            time += video.duration

            heapq.heappush(self.user_playtime, (time, userid))

        return ret
