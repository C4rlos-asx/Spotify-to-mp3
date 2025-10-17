import os
import re
from urllib.parse import urlparse
import sys
import time
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple, Callable

import requests
import tempfile
import unicodedata
from io import BytesIO
try:
    from PIL import Image  # type: ignore
    import imagehash  # type: ignore
    _HAS_IMAGEHASH = True
except Exception:
    _HAS_IMAGEHASH = False
from dotenv import load_dotenv
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL


SPOTIFY_URL_RE = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist)/([a-zA-Z0-9]+)")


@dataclass
class TrackMeta:
    title: str
    artists: List[str]
    album: str
    cover_url: Optional[str]
    track_number: Optional[int] = None
    duration_ms: Optional[int] = None


def load_env() -> None:
    # Load .env if present
    load_dotenv()


def get_spotify_client() -> spotipy.Spotify:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET. Create a .env from .env.example.")
    auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    return spotipy.Spotify(auth_manager=auth_manager)


def parse_spotify_url(url: str) -> Tuple[str, str]:
    # Try regex first (handles optional intl-xx segment)
    m = SPOTIFY_URL_RE.match(url)
    if m:
        return m.group(1), m.group(2)

    # Fallback robust parsing using urllib.parse
    try:
        u = urlparse(url)
        if u.netloc not in ("open.spotify.com",):
            raise ValueError("host")
        # Split path parts, ignoring leading '/'
        parts = [p for p in u.path.split('/') if p]
        # Remove optional locale segment like 'intl-es'
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) < 2:
            raise ValueError("parts")
        kind = parts[0]
        sid = parts[1]
        if kind not in ("track", "album", "playlist"):
            raise ValueError("kind")
        # Strip any additional fragments (id might already be clean)
        sid = sid.split('?')[0]
        return kind, sid
    except Exception:
        raise ValueError("URL no válida de Spotify. Debe ser track/album/playlist.")


def fetch_tracks(sp: spotipy.Spotify, kind: str, sid: str) -> List[TrackMeta]:
    tracks: List[TrackMeta] = []

    def to_meta(item, track_number=None):
        name = item["name"]
        artists = [a["name"] for a in item["artists"]]
        album = item["album"]["name"] if item.get("album") else ""
        images = item["album"]["images"] if item.get("album") else []
        cover_url = images[0]["url"] if images else None
        duration_ms = item.get("duration_ms")
        return TrackMeta(title=name, artists=artists, album=album, cover_url=cover_url, track_number=track_number, duration_ms=duration_ms)

    if kind == "track":
        t = sp.track(sid)
        tracks.append(to_meta(t, t.get("track_number")))
    elif kind == "album":
        album = sp.album(sid)
        album_name = album["name"]
        images = album.get("images", [])
        cover_url = images[0]["url"] if images else None
        results = sp.album_tracks(sid, limit=50, offset=0)
        offset = 0
        while True:
            for t in results["items"]:
                meta = TrackMeta(
                    title=t["name"],
                    artists=[a["name"] for a in t["artists"]],
                    album=album_name,
                    cover_url=cover_url,
                    track_number=t.get("track_number"),
                    duration_ms=t.get("duration_ms"),
                )
                tracks.append(meta)
            if results.get("next"):
                offset += results["limit"]
                results = sp.album_tracks(sid, limit=50, offset=offset)
            else:
                break
    elif kind == "playlist":
        # Paginate through playlist items
        results = sp.playlist_items(sid, additional_types=("track",), limit=100, offset=0)
        offset = 0
        while True:
            for it in results["items"]:
                t = it.get("track")
                if not t or t.get("is_local"):
                    continue
                album = t.get("album") or {}
                images = album.get("images", [])
                cover_url = images[0]["url"] if images else None
                tracks.append(
                    TrackMeta(
                        title=t.get("name", ""),
                        artists=[a.get("name", "") for a in t.get("artists", [])],
                        album=album.get("name", ""),
                        cover_url=cover_url,
                        track_number=t.get("track_number"),
                        duration_ms=t.get("duration_ms"),
                    )
                )
            if results.get("next"):
                offset += results["limit"]
                results = sp.playlist_items(sid, additional_types=("track",), limit=100, offset=offset)
            else:
                break
    else:
        print("ERROR: Tipo no soportado:", kind)
        sys.exit(1)

    return tracks


def sanitize_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name)


def ensure_ffmpeg_available():
    # yt-dlp relies on ffmpeg in PATH for audio conversion
    from shutil import which

    if which("ffmpeg") is None:
        print("ADVERTENCIA: ffmpeg no encontrado en PATH. La conversión a MP3 puede fallar.")
        print("Instala ffmpeg y reinicia la terminal: https://ffmpeg.org/download.html")


def _gather_strict_candidate_urls(meta: TrackMeta, max_results: int = 50) -> List[str]:
    """Return ordered video URLs that strictly match title+artist.
    First try query "Artists - Title" then fallback to title-only query, enforcing
    that the entry contains all title tokens and at least one artist token in
    title or channel. Uses flat extraction for speed.
    """
    target_title = _normalize_text(meta.title)
    artist_names = [_normalize_text(a) for a in meta.artists]

    ydl_opts_flat = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "extract_flat": True,
    }

    def ytdl_search(q: str) -> list:
        try:
            with YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(f"ytsearch{max_results}:{q}", download=False)
                return info.get("entries", []) if info else []
        except Exception:
            return []

    def has_title_and_artist(entry) -> bool:
        title = _normalize_text(entry.get("title") or "")
        channel = _normalize_text(entry.get("channel") or "")
        title_ok = all(tok in title for tok in target_title.split()) if target_title else False
        artist_ok = any(a in title or a in channel for a in artist_names if a)
        return bool(title_ok and artist_ok)

    def entry_url(e) -> Optional[str]:
        url = e.get("webpage_url") or e.get("url")
        if not url:
            return None
        if "://" not in url:
            url = f"https://www.youtube.com/watch?v={url}"
        return url

    artists_joined = ", ".join(meta.artists)
    q2 = f"{artists_joined} - {meta.title}"
    q1 = f"{meta.title}"

    ordered: List[str] = []
    for q in (q2, q1):
        for e in ytdl_search(q):
            if has_title_and_artist(e):
                u = entry_url(e)
                if u and u not in ordered:
                    ordered.append(u)
    return ordered


def yt_search_query(meta: TrackMeta) -> str:
    # Prefer a clean query that matches typical video titles
    return f"{', '.join(meta.artists)} - {meta.title}"


def _normalize_text(s: str) -> str:
    # Lowercase, strip accents, remove punctuation-like chars for robust matching
    s = s.lower()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\-–—_·•·,:;!?.'\"]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_duration_match(target_ms: Optional[int], video_seconds: Optional[float]) -> bool:
    """Return True if video's duration matches Spotify duration within tolerance.
    Tolerance: max(8s, 6% of target). Also reject if video > target*2 or target + 120s.
    If target is None or video_seconds is None, return True (cannot judge).
    """
    if not target_ms or video_seconds is None:
        return True
    target_s = max(1.0, float(target_ms) / 1000.0)
    diff = abs(video_seconds - target_s)
    tol = max(8.0, 0.06 * target_s)
    if video_seconds > max(target_s * 2.0, target_s + 120.0):
        return False
    return diff <= tol

def _phash_from_url(url: str, timeout: float = 8.0):
    if not _HAS_IMAGEHASH:
        return None
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        with Image.open(BytesIO(r.content)) as im:
            im = im.convert('RGB')
            return imagehash.phash(im)
    except Exception:
        return None


def _pick_best_youtube_by_title(meta: TrackMeta, max_results: int = 10, cover_url: Optional[str] = None) -> Optional[str]:
    target_title = _normalize_text(meta.title)
    artist_names = [_normalize_text(a) for a in meta.artists]
    bad_words = ["live", "cover", "karaoke", "sped up", "nightcore", "slowed", "8d", "lyrics", "lyric"]

    # Flat extraction is faster and sufficient for stages 1 and 2
    ydl_opts_flat = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "extract_flat": True,
    }
    # Full extraction (thumbnails) only when needed (stage 3)
    ydl_opts_full = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
    }

    def ytdl_search(q: str, flat: bool = True) -> list:
        try:
            with YoutubeDL(ydl_opts_flat if flat else ydl_opts_full) as ydl:
                info = ydl.extract_info(f"ytsearch{max_results}:{q}", download=False)
                return info.get("entries", []) if info else []
        except Exception:
            return []

    def has_title_and_artist(entry) -> Tuple[bool, bool]:
        title = _normalize_text(entry.get("title") or "")
        channel = _normalize_text(entry.get("channel") or "")
        title_ok = all(tok in title for tok in target_title.split()) if target_title else False
        artist_ok = any(a in title or a in channel for a in artist_names if a)
        return title_ok, artist_ok

    def phash_best_distance(entry, spotify_hash) -> Optional[int]:
        if spotify_hash is None:
            return None
        try:
            thumbs = entry.get('thumbnails') or []
            # Use only one likely high-res thumbnail to save time
            thumb_url = None
            if isinstance(thumbs, list) and thumbs:
                last = thumbs[-1]
                thumb_url = last.get('url') if isinstance(last, dict) else None
            if not thumb_url:
                return None
            h = _phash_from_url(thumb_url, timeout=6.0)
            if h is None:
                return None
            return (spotify_hash - h)
        except Exception:
            return None

    def score(entry, spotify_hash=None) -> float:
        title = _normalize_text(entry.get("title") or "")
        channel = _normalize_text(entry.get("channel") or "")
        title_ok, artist_ok = has_title_and_artist(entry)

        base = 0.0
        if not title_ok:
            base += 50.0
        if not artist_ok:
            base += 50.0

        for w in bad_words:
            if w in title:
                base += 15.0

        if "topic" in channel:
            base -= 5.0
        if "official audio" in title:
            base -= 3.0

        if spotify_hash is not None:
            best_dist = phash_best_distance(entry, spotify_hash)
            if best_dist is not None:
                base += float(best_dist) * 1.5

        length_bias = float(len(title)) * 0.02
        return base + length_bias

    # Prepare queries for stages
    artists_joined = ", ".join(meta.artists)
    q_stage2 = f"{artists_joined} - {meta.title}"
    q_stage1 = f"{meta.title}"
    q_stage3 = q_stage2

    # Stage 2: title + artist, require both (prefer this stage)
    entries2 = ytdl_search(q_stage2, flat=True)
    strict2 = []
    for e in entries2:
        t_ok, a_ok = has_title_and_artist(e)
        # duration filter (when available)
        dur_ok = _is_duration_match(meta.duration_ms, float(e.get("duration")) if e.get("duration") is not None else None)
        if t_ok and a_ok and dur_ok:
            strict2.append(e)
    if strict2:
        # Early exit: return immediately if we find a Topic/official audio to minimize extra work
        for e in strict2:
            title = _normalize_text(e.get("title") or "")
            channel = _normalize_text(e.get("channel") or "")
            if ("topic" in channel) or ("official audio" in title):
                return e.get("webpage_url") or e.get("url")
        best2 = min(strict2, key=lambda e: score(e))
        return best2.get("webpage_url") or best2.get("url")

    # Stage 1: title only, still require artist + title
    entries1 = ytdl_search(q_stage1, flat=True)
    strict1 = []
    for e in entries1:
        t_ok, a_ok = has_title_and_artist(e)
        dur_ok = _is_duration_match(meta.duration_ms, float(e.get("duration")) if e.get("duration") is not None else None)
        if t_ok and a_ok and dur_ok:
            strict1.append(e)
    if strict1:
        best1 = min(strict1, key=lambda e: score(e))
        return best1.get("webpage_url") or best1.get("url")

    # Stage 3: title + artist + cover similarity enforcement
    spotify_hash = _phash_from_url(cover_url, timeout=6.0) if cover_url else None
    # Need full extraction to access thumbnails reliably
    entries3 = ytdl_search(q_stage3, flat=False)
    strict3 = []
    COVER_MAX_DIST = 10
    for e in entries3:
        t_ok, a_ok = has_title_and_artist(e)
        if not (t_ok and a_ok):
            continue
        # duration guard for full extraction entries
        dur_ok = _is_duration_match(meta.duration_ms, float(e.get("duration")) if e.get("duration") is not None else None)
        if not dur_ok:
            continue
        if spotify_hash is None:
            continue
        best_dist = phash_best_distance(e, spotify_hash)
        if best_dist is not None and best_dist <= COVER_MAX_DIST:
            strict3.append(e)
    if strict3:
        best3 = min(strict3, key=lambda e: score(e, spotify_hash))
        return best3.get("webpage_url") or best3.get("url")

    return None


def trim_to_spotify_duration(mp3_path: str, duration_ms: Optional[int]) -> Optional[str]:
    if not duration_ms:
        return mp3_path
    seconds = max(0.0, duration_ms / 1000.0)
    base, _ = os.path.splitext(mp3_path)
    out_path = base + ".trim.mp3"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", mp3_path,
            "-t", f"{seconds:.3f}",
            "-c", "copy",
            out_path,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.replace(out_path, mp3_path)
        except Exception:
            return out_path
        return mp3_path
    except Exception:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        return mp3_path

def download_and_convert(
    meta: TrackMeta,
    out_dir: str,
    retries: int = 2,
    base_filename: Optional[str] = None,
    use_cover_match: bool = False,
    verbose: bool = False,
    log: Optional[Callable[[str], None]] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    twofactor: Optional[str] = None,
    usenetrc: bool = False,
) -> Optional[str]:
    ensure_ffmpeg_available()

    base_name = base_filename or sanitize_filename(f"{', '.join(meta.artists)} - {meta.title}")
    outtmpl = os.path.join(out_dir, base_name + ".%(ext)s")

    # Skip if final mp3 already exists
    precheck_mp3 = os.path.join(out_dir, base_name + ".mp3")
    if os.path.exists(precheck_mp3):
        return precheck_mp3

    class _YDLLogger:
        def __init__(self, cb: Optional[Callable[[str], None]]):
            self.cb = cb
        def debug(self, msg):
            if self.cb and verbose:
                try:
                    self.cb(str(msg))
                except Exception:
                    pass
        def warning(self, msg):
            if self.cb:
                try:
                    self.cb(f"WARN: {msg}")
                except Exception:
                    pass
        def error(self, msg):
            if self.cb:
                try:
                    self.cb(f"ERROR: {msg}")
                except Exception:
                    pass

    ydl_logger = _YDLLogger(log)

    # Resolve authentication preferences (no cookies supported)
    use_auth = bool(username or usenetrc)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ],
        "quiet": (not verbose),
        "no_warnings": (not verbose),
        "verbose": bool(verbose),
        "logger": ydl_logger,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "default_search": "ytsearch",
        "cachedir": False,
        "quiet": not bool(verbose),
        "no_warnings": not bool(verbose),
        "verbose": bool(verbose),
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
            }
        },
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": "exponential_backoff"},
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "concurrent_fragment_downloads": 1,
        # "sleep_interval": 1.0,
        # "max_sleep_interval": 3.0,
        "geo_bypass": True,
    }
    # yt-dlp authentication options
    # Do not set username/password for YouTube (not supported); we still allow for other sites
    youtube_domain = "youtube.com"
    if use_auth:
        if usenetrc:
            ydl_opts["usenetrc"] = True
        if username:
            ydl_opts["username"] = username
        if password:
            ydl_opts["password"] = password
        if twofactor:
            ydl_opts["twofactor"] = twofactor

    query = yt_search_query(meta)
    chosen_url = _pick_best_youtube_by_title(meta, cover_url=meta.cover_url if use_cover_match else None)

    # Build list of candidate URLs to try in order
    candidates: List[str] = []
    if chosen_url:
        candidates.append(chosen_url)
    for u in _gather_strict_candidate_urls(meta):
        if u not in candidates:
            candidates.append(u)
    # As a last resort, allow yt-dlp to run the search query itself
    if not candidates:
        candidates.append(query)

    try:
        for cand in candidates:
            used_android_retry = False
            # Preflight: check candidate duration via metadata (no download)
            try:
                info_opts = {
                    "quiet": (not verbose),
                    "no_warnings": (not verbose),
                    "verbose": bool(verbose),
                    "logger": ydl_logger,
                    "noplaylist": True,
                    "default_search": "ytsearch",
                    "cachedir": False,
                    # usar android también para preflight
                    "extractor_args": {"youtube": {"player_client": ["android"]}},
                }
                with YoutubeDL(info_opts) as ydl:
                    meta_info = ydl.extract_info(cand, download=False)
                    if "entries" in meta_info:
                        meta_info = meta_info["entries"][0]
                    vid_dur = float(meta_info.get("duration")) if meta_info and meta_info.get("duration") is not None else None
                    if not _is_duration_match(meta.duration_ms, vid_dur):
                        # Skip this candidate if duration is clearly off
                        continue
            except Exception:
                # If preflight fails, proceed to attempts as usual
                pass

            for attempt in range(retries + 1):
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(cand, download=True)
                        if "entries" in info:
                            info = info["entries"][0]
                        downloaded = ydl.prepare_filename(info)
                        mp3_path = os.path.splitext(downloaded)[0] + ".mp3"
                        if os.path.exists(mp3_path):
                            return mp3_path
                        fallback = os.path.join(out_dir, base_name + ".mp3")
                        if os.path.exists(fallback):
                            return fallback
                except Exception as e:
                    msg = str(e).lower()
                    # Broaden detection to cover YouTube anti-bot / verification prompts
                    bot_checks = [
                        "sign in to confirm you're not a bot",
                        "sign in to confirm you’re not a bot",
                        "confirm you are not a bot",
                        "verify that you're not a bot",
                        "verification required",
                        "this video may be inappropriate",
                        "age-restricted",
                    ]
                    is_age_block = (
                        ("confirm your age" in msg)
                        or ("inappropriate for some users" in msg)
                        or ("age" in msg and "restricted" in msg)
                        or any(b in msg for b in bot_checks)
                    )
                    # Consider Chrome DB copy error and DPAPI decryption as cookie browser failures
                    dpapi_fail = (
                        ("failed to decrypt with dpapi" in msg)
                        or ("could not copy chrome cookie database" in msg)
                    )
                    # Try Android player client first to bypass some age prompts
                    if is_age_block and not used_android_retry:
                        try:
                            opts_android = dict(ydl_opts)
                            opts_android["extractor_args"] = {"youtube": {"player_client": ["android"]}}
                            with YoutubeDL(opts_android) as ydl:
                                info = ydl.extract_info(cand, download=True)
                                if "entries" in info:
                                    info = info["entries"][0]
                                downloaded = ydl.prepare_filename(info)
                                mp3_path = os.path.splitext(downloaded)[0] + ".mp3"
                                if os.path.exists(mp3_path):
                                    return mp3_path
                                fallback = os.path.join(out_dir, base_name + ".mp3")
                                if os.path.exists(fallback):
                                    return fallback
                        except Exception:
                            pass
                        finally:
                            used_android_retry = True
                    # If still blocked or DPAPI, try next candidate
                    if is_age_block or dpapi_fail:
                        # Stop attempts for this candidate and try the next one
                        break
                    if attempt < retries:
                        time.sleep(2.5 * (attempt + 1))
                    # Continue to next attempt for this candidate
                    continue
        # move to next candidate after exhausting attempts or breaking due to age issues
        return None
    finally:
        pass


def embed_tags(mp3_path: str, meta: TrackMeta) -> None:
    try:
        audio = EasyID3(mp3_path)
    except ID3NoHeaderError:
        audio = EasyID3()
        audio.save(mp3_path)
        audio = EasyID3(mp3_path)

    audio["title"] = meta.title
    if meta.artists:
        audio["artist"] = ", ".join(meta.artists)
    if meta.album:
        audio["album"] = meta.album
    if meta.track_number:
        audio["tracknumber"] = str(meta.track_number)
    audio.save()

    # Cover art
    if meta.cover_url:
        try:
            img = requests.get(meta.cover_url, timeout=15)
            img.raise_for_status()
            with open(mp3_path, "rb+") as f:
                pass
            tags = ID3(mp3_path)
            mime = "image/jpeg" if meta.cover_url.lower().endswith("jpg") or meta.cover_url.lower().endswith("jpeg") else "image/png"
            tags.add(
                APIC(
                    encoding=3,  # utf-8
                    mime=mime,
                    type=3,  # front cover
                    desc=u"Cover",
                    data=img.content,
                )
            )
            tags.save(v2_version=3)
        except Exception:
            # Non-fatal if cover fails
            pass




def process_url(
    url: str,
    out_dir: str,
    trim_to_spotify: bool = False,
    log: Callable[[str], None] = print,
    verbose: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    twofactor: Optional[str] = None,
    usenetrc: bool = False,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    load_env()
    sp = get_spotify_client()
    kind, sid = parse_spotify_url(url)
    tracks = fetch_tracks(sp, kind, sid)

    # Determine container subfolder for album/playlist
    container_dir = out_dir
    if kind == "album":
        alb = sp.album(sid)
        container_name = sanitize_filename(alb.get("name", "album"))
        container_dir = os.path.join(out_dir, container_name)
    elif kind == "playlist":
        pl = sp.playlist(sid, fields="name")
        container_name = sanitize_filename(pl.get("name", "playlist"))
        container_dir = os.path.join(out_dir, container_name)

    if container_dir != out_dir:
        os.makedirs(container_dir, exist_ok=True)

    log(f"Encontradas {len(tracks)} pistas para descargar…")
    if verbose:
        log(f"Destino: {container_dir}")

    failures = []  # collect failed items to report at the end

    for idx, t in enumerate(tracks, start=1):
        # Build base filename with prefix
        prefix = None
        if kind == "album" and t.track_number:
            prefix = f"{t.track_number:02d} "
        elif kind == "playlist":
            prefix = f"{idx:02d} "

        display_name = f"{', '.join(t.artists)} - {t.title}"
        base_filename = sanitize_filename((prefix or "") + display_name)

        log(f"[{idx}/{len(tracks)}] {display_name}")
        # Use cover-art matching only for albums/tracks (not playlists)
        use_cover = kind in ("album", "track")
        mp3_path = download_and_convert(
            t,
            container_dir,
            base_filename=base_filename,
            use_cover_match=use_cover,
            verbose=verbose,
            log=log,
            username=username,
            password=password,
            twofactor=twofactor,
            usenetrc=usenetrc,
        )
        if not mp3_path:
            # stop processing this track immediately and record failure
            log("  Saltado: descarga fallida")
            failures.append(display_name)
            continue

        # success path: tag, then optional trim
        embed_tags(mp3_path, t)
        if trim_to_spotify:
            if verbose and t.duration_ms:
                log(f"  Recortando a {t.duration_ms/1000.0:.2f}s (Spotify)…")
            trim_to_spotify_duration(mp3_path, t.duration_ms)

        # Delay between tracks to avoid anti-bot/rate limiting
        try:
            time.sleep(8.0)
        except Exception:
            pass

    # Print only failures at the end
    if failures:
        log("\nResumen: pistas que fallaron")
        for name in failures:
            log(f" - {name}")


# CLI removido: este módulo ahora se usa como librería desde la app web.
