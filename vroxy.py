from __future__ import unicode_literals
from configparser import ConfigParser
from asyncio import sleep
import time
import re
import random
from os import path
import os
from typing import Optional
from urllib.parse import urlparse
from pathlib import Path

from aiohttp import web
from whitelist import DomainWhitelist, load_list
from yt_dlp import YoutubeDL

from normalize import normalizeUrl


class PoolCount:
    def __init__(self):
        self.count = 0

    def add(self):
        self.count += 1

    def remove(self):
        self.count -= 1

class Item:
    def __init__(self, url, sort):
        self.original_url = url
        self.hostname = urlparse(url).hostname
        self.resolved_url = None
        self.resolved_id = None
        self.resolved_format = None
        self.sort = sort
        self.expiry = 0
        self.lastAccess = 0
        self.processing = True

    def resolve(self, f) -> None:
        if self.sort:
            f = f["formats"][-1]
        self.resolved_url = f["url"]
        self.resolved_id = f["format_id"]
        self.resolved_format = f["format"]
        self.expiry = self.extractExpiry()
        self.processing = False

    def extractExpiry(self) -> float:
        # default to 10s for m3u8 links as they will force an improper starting time if the cache is used for too long
        # allows 10s for handling a burst of users requesting the same URL (ie: someone just queued a new vid)
        # if ".m3u8" in self.resolved_url:
        #     return time.time() + 10  
        p = expire_regex.search(self.resolved_url)
        if p is not None: return int(p.group(1))
        return time.time() + 600  # default to 10 minute

expire_regex = re.compile(r"exp(?:ir(?:es?|ation))?=(\d+)")
nextGCTime = time.time() + 3600
cache_map = {}
pool_max = 10
pool = PoolCount()
routes = web.RouteTableDef()

config = ConfigParser()
config["server"] = {
    "host": os.getenv("VROXY_HOST", "0.0.0.0"),
    "port": os.getenv("PORT", "8008"),
    "whitelist": os.getenv("VROXY_WHITELIST", ""),
}
if path.isfile(path.join(path.dirname(__file__), "settings.ini")): config.read(path.join(path.dirname(__file__), "settings.ini"))

domain_whitelist: Optional[DomainWhitelist] = None

if wl_path := config["server"]["whitelist"]:
    domain_whitelist = load_list(wl_path)

mode_map = {
    # default
    "0": 0,
    # avhigh
    "1": 1,
    # avlow
    "2": 2,
    # hqvidcompat
    "3": 3,
    # hqvidbest
    "4": 4,
}
sort_opts = {
    # decent sized media with audio+video, generally compatible with all platforms
    0: ["proto:https", "hasvid", "hasaud", "res:1440"],
    # sort preferring audio+video with the highest quality
    1: ["hasvid", "hasaud", "res"],
    # sort preferring audio+video with the lowest quality
    2: ["hasvid", "hasaud", "+res"],
    # sort preferring highest quality with priority on VP9 codec for platform compatibility
    3: ["codec:vp9", "hasvid", "res"],
    # sort preferring highest quality without concern for codec or audio
    4: ["hasvid", "res"],
}

@routes.view("/healthz")
class Health(web.View):
    async def get(self):
        return web.Response(text="OK")

@routes.view("/")
class YTDLProxy(web.View):
    async def head(self):
        if not self.request.query.get("url") and not self.request.query.get("u"):
            return web.Response(status=404)
        return await self.process()

    async def get(self):
        if not self.request.query.get("url") and not self.request.query.get("u"):
            return web.Response(status=404, text="Missing Url Param")
        return await self.process()

    async def process(self):
        userUrl = self.getUserUrl()
        if domain_whitelist and not domain_whitelist.allows(userUrl):
            return web.Response(status=403, text="Domain not in whitelist")

        url = await self.resolveUrl(userUrl)

        if not url:
            return web.Response(status=408)

        return web.Response(status=307, headers={"Location": url})

    def getUserUrl(self) -> str:
        return normalizeUrl(self.request.query.get("url") or self.request.query.get("u"))

    async def resolveUrl(self, url: str) -> str:
        rid = random.getrandbits(16)
        global nextGCTime
        curTime = time.time()
        # clean up the cache every hour
        if curTime > nextGCTime:
            nextGCTime = time.time() + 3600
            purge = []
            for cache_id, cache_item in cache_map.items():
                # if the item is expired or was last accessed over an hour ago, purge
                if cache_item.lastAccess + 3600 < curTime or cache_item.expiry < curTime:
                    purge.append(cache_id)
            for purge_id in purge:
                del cache_map[purge_id]

        # silence the output of ytdl
        ytdl_opts = {"quiet": True}

        mode = mode_map[self.request.query.get("m") or "0"]
        fid = self.request.query.get("f")
        host = urlparse(url).hostname
        # if format ID is provided, retrieve that explicitly
        if fid:
            ytdl_opts["format"] = fid
            cacheId = fid
            sort = None
        # otherwise use the "best" sorting based on the sort available
        else:
            # use either the given user sort, or extrapolate for the given preset mode
            s = self.request.query.get("s")
            if (s): sort = s.replace(" ", "").split(",")
            else: sort = list(sort_opts[mode])
            try: 
                if host.index("vimeo") > -1:
                    sort.append("proto:m3u8_native")
            except: pass
            ytdl_opts["format_sort"] = sort
            cacheId = ",".join(sort)

        _id = f"{cacheId}~{url}"
        if _id in cache_map:
            item = cache_map[_id]
            if item.expiry < curTime:
                print(f"[{rid}] Cache expired")
                del cache_map[_id]
            else:
                # wait until the other request for the same url resolves,
                # then use the cached url from that
                while item.processing:
                    await sleep(1)
                print(f"[{rid}] Resolving '{cacheId}' for url: {url}")
                print(f"[{rid}] Cache hit")
                print(item.resolved_url)
                print(
                    f"[{rid}] {item.resolved_format} expires in {item.expiry - curTime} seconds", flush=True
                )
                item.lastAccess = curTime
                return item.resolved_url or ""

        cache_map[_id] = item = Item(url, sort)

        # wait for an pool slot to open
        timeout = curTime + 30  # 30 seconds timelimit for waiting
        while pool.count >= pool_max:
            if curTime > timeout: return None
            await sleep(1)
        with YoutubeDL(ytdl_opts) as ytdl:
            print(f"[{rid}] Resolving '{cacheId}' for url: {url}")
            print(f"[{rid}] Fetching fresh info", flush=True)
            pool.add()
            result = ytdl.extract_info(url, download=False)
            # print(result.keys())
            item.resolve(result)
            pool.remove()
            print(f"[{rid}] {item.resolved_url}")
            print(f"[{rid}] {item.resolved_format} expires in {item.expiry - curTime} seconds", flush=True)

        item.lastAccess = curTime
        return item.resolved_url or ""


app = web.Application()
app.add_routes(routes)
print("Starting Vroxy server.")
if os.environ.get("TMUX"):
    print("--- TMUX USAGE REMINDER ---")
    print("If the service is running in a TMUX instance, you can exit without killing the service with CTRL+B and then press D")
    print("If you run the CTRL+C command, you will kill the service making your urls return 502.")
    print(f"Remember you can restart the service by exiting the TMUX instance with CTRL+B and then D, then run 'bash {path.dirname(__file__)}/vroxy_reload.sh'", flush=True)
web.run_app(app, host=config["server"]["host"], port=config["server"]["port"])
