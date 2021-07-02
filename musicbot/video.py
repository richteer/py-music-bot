import youtube_dl as ytdl
import discord
import json
import logging

YTDL_OPTS = {
    "default_search": "ytsearch",
    "format": "bestaudio/best",
    "quiet": True,
    "extract_flat": "in_playlist"
}

# TODO: create if doesn't exist or skip probably
video_cache = {}
try:
    with open("video_cache.json", "r") as f:
        video_cache = json.loads(f.read())
        logging.info("Video cache loaded successfully")
except:
    logging.warn("Video cache failed to load, ignore if first run")

class Video:
    """Class containing information about a particular video."""

    def __init__(self, url_or_search, requested_by):
        """Plays audio from (or searches for) a URL."""
        with ytdl.YoutubeDL(YTDL_OPTS) as ydl:
            video = self._get_info(url_or_search)
            video_format = video["formats"][0]
            self.stream_url = video_format["url"]
            self.video_url = video["webpage_url"]
            self.title = video["title"]
            self.uploader = video["uploader"] if "uploader" in video else ""
            self.thumbnail = video[
                "thumbnail"] if "thumbnail" in video else None
            self.requested_by = requested_by
            self.duration = video["duration"]

    def _get_info(self, video_url):
        # TODO: may need to strip off other &params too here
        if video := video_cache.get(video_url.split("v=")[-1]):
            logging.info(f"Video info for {video['id']} retrieved from cache")
            return video

        with ytdl.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(video_url, download=False)
            video = None
            if "_type" in info and info["_type"] == "playlist":
                video = self._get_info(
                    info["entries"][0]["url"])  # get info for first video
            else:
                video = info

            video_cache[video["id"]] = video

            # TODO: have some kind of cache manager instead for video objects
            #  This is probably going to perform very poorly as the cache grows
            #  Maybe load it one at the beginning, and either periodically flush
            #  or just dump it at termination time
            with open("video_cache.json", "w") as f:
                f.write(json.dumps(video_cache))
            return video

    def get_embed(self):
        """Makes an embed out of this Video's information."""
        embed = discord.Embed(
            title=self.title, description=self.uploader, url=self.video_url)
        embed.set_footer(
            text=f"Requested by {self.requested_by.display_name}",
            icon_url=self.requested_by.avatar_url)
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        return embed

class Setlist(list):
    """Class containing information about a user's setlist"""
    def __init__(self, url, requester):
        with ytdl.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            for vid in info["entries"]:
                self.append(f"https://youtu.be/{vid['id']}")
