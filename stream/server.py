"""
HTTP streaming server that runs inside the same asyncio loop as the bot.

Endpoints
---------
GET /stream/<token>            — stream media or embedded HLS player
GET /stream/<token>/<filename> — same, with a human-readable filename in the URL
GET /healthz                   — liveness probe
"""
from __future__ import annotations

import logging
import secrets
import time

import aiohttp
from aiohttp import web

from .resolver import resolve_stream_url

logger = logging.getLogger(__name__)

# ── Token store ───────────────────────────────────────────────────────────────

_store: dict[str, dict] = {}          # token  → entry
_rcache: dict[str, dict] = {}         # token  → resolved (cached 1 h)


def create_stream_token(
    url: str,
    filename: str = "stream",
    mime: str = "video/mp4",
    title: str = "",
    ttl: int = 86400,
) -> str:
    """Register *url* in the token store and return the opaque token string."""
    token = secrets.token_urlsafe(16)
    _store[token] = {
        "url":        url,
        "filename":   filename,
        "mime":       mime,
        "title":      title or filename,
        "expires_at": time.time() + ttl,
    }
    _purge()
    return token


def _purge() -> None:
    now = time.time()
    dead = [t for t, v in _store.items() if now > v["expires_at"]]
    for t in dead:
        _store.pop(t, None)
        _rcache.pop(t, None)


# ── Embedded HLS player page ──────────────────────────────────────────────────

def _hls_page(src: str, title: str) -> str:
    te = title.replace('"', "&quot;").replace("<", "&lt;")
    se = src.replace('"', "&quot;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{te}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0 }}
body {{ background: #000; color: #eee; font-family: system-ui, sans-serif;
       display: flex; flex-direction: column; align-items: center;
       justify-content: center; min-height: 100vh; }}
h2 {{ margin: 10px 16px; font-size: .95rem; max-width: 95vw;
     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
video {{ width: 100%; max-height: 92vh; }}
</style>
</head>
<body>
<h2>{te}</h2>
<video id="v" controls autoplay playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest/dist/hls.min.js"></script>
<script>
var v = document.getElementById('v');
var s = "{se}";
if (typeof Hls !== 'undefined' && Hls.isSupported()) {{
  var h = new Hls({{enableWorker:true}});
  h.loadSource(s); h.attachMedia(v);
}} else if (v.canPlayType('application/vnd.apple.mpegurl')) {{
  v.src = s;
}} else {{
  document.body.innerHTML =
    '<p style="padding:2rem">HLS not supported in this browser.<br>' +
    '<a href="' + s + '" style="color:#4af">Direct link</a></p>';
}}
</script>
</body>
</html>"""


# ── Route handlers ────────────────────────────────────────────────────────────

async def _handle(request: web.Request) -> web.StreamResponse | web.Response:
    token = request.match_info["token"]
    entry = _store.get(token)

    if not entry:
        return web.Response(status=404, text="Stream not found or link has expired.\n")
    if time.time() > entry["expires_at"]:
        _store.pop(token, None)
        return web.Response(status=410, text="Stream link has expired.\n")

    # Resolve (cached for 1 hour so CDN-signed URLs get refreshed on next visit)
    resolved = _rcache.get(token)
    if not resolved or time.time() > resolved.get("_until", 0):
        try:
            resolved = await resolve_stream_url(entry["url"])
            resolved["_until"] = time.time() + 3600
            _rcache[token] = resolved
        except Exception as exc:
            logger.exception("Resolve failed for token %s", token)
            return web.Response(status=502, text=f"Could not resolve stream: {exc}\n")

    stream_url: str = resolved["url"]
    is_hls: bool    = resolved.get("is_hls", False) or ".m3u8" in stream_url
    title: str      = resolved.get("title") or entry.get("title") or "stream"
    filename: str   = entry.get("filename") or f"stream.{resolved.get('ext', 'mp4')}"

    # ── HLS → embedded player page ───────────────────────────────────────────
    if is_hls:
        return web.Response(
            status=200,
            content_type="text/html",
            charset="utf-8",
            body=_hls_page(stream_url, title).encode(),
        )

    # ── Direct media → transparent byte proxy with Range support ─────────────
    range_hdr = request.headers.get("Range")
    up_hdrs: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "*/*",
        "Accept-Encoding": "identity",
    }
    if range_hdr:
        up_hdrs["Range"] = range_hdr

    timeout   = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    connector = aiohttp.TCPConnector(limit=32)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.get(stream_url, headers=up_hdrs, allow_redirects=True) as up:
                if up.status not in (200, 206):
                    return web.Response(
                        status=up.status,
                        text=f"Upstream returned HTTP {up.status}\n",
                    )

                out_hdrs = {
                    "Content-Type":         up.headers.get("Content-Type",
                                            resolved.get("mime", "video/mp4")),
                    "Accept-Ranges":        "bytes",
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition":  f'inline; filename="{filename}"',
                }
                for hdr in ("Content-Length", "Content-Range", "Cache-Control", "ETag"):
                    if hdr in up.headers:
                        out_hdrs[hdr] = up.headers[hdr]

                resp = web.StreamResponse(status=up.status, headers=out_hdrs)
                await resp.prepare(request)
                try:
                    async for chunk in up.content.iter_chunked(131_072):  # 128 KB
                        await resp.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass  # client disconnected — that's fine
                await resp.write_eof()
                return resp

    except aiohttp.ClientError as exc:
        logger.warning("Proxy error for token %s: %s", token, exc)
        return web.Response(status=502, text=f"Proxy error: {exc}\n")
    except Exception as exc:
        logger.exception("Unexpected error proxying token %s", token)
        return web.Response(status=500, text=f"Internal error: {exc}\n")


async def _healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


# ── App factory + server lifecycle ────────────────────────────────────────────

def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/stream/{token}",           _handle)
    app.router.add_get("/stream/{token}/{filename}", _handle)
    app.router.add_get("/healthz",                  _healthz)
    return app


async def start_stream_server(port: int) -> web.AppRunner:
    """Start the HTTP streaming server on *port* and return its runner."""
    app    = _build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Stream server listening on 0.0.0.0:%d", port)
    return runner
