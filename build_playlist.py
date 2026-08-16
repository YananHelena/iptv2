#!/usr/bin/env python3
"""Build the curated Turkey TV M3U with stream health checks.

Priority for each configured channel:
1) iptv-org public streams (best-scored first; up to 3 candidates)
2) curated public/free fallbacks in extra_streams.json
3) explicitly configured official/public live pages resolved with yt-dlp
4) enabled remote free/FTA fallback M3U sources

Before a URL is written to the final M3U, the builder probes it. For HLS it checks:
- the manifest is reachable and looks like M3U8,
- a media playlist can be reached when the URL is a master playlist,
- an encryption key / init segment (if present) is reachable,
- at least one media segment returns data.

This catches common 403/dead-manifest failures. It cannot reliably detect a stream
that technically serves valid video but visually shows a black frame/slate.
"""
import csv
import html
import json
import os
import re
import socket
import subprocess
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

API_BASE = os.getenv("IPTV_API_BASE", "https://iptv-org.github.io/api")
CHANNELS_URL = os.getenv("IPTV_CHANNELS_URL", f"{API_BASE}/channels.json")
STREAMS_URL = os.getenv("IPTV_STREAMS_URL", f"{API_BASE}/streams.json")
LOGOS_URL = os.getenv("IPTV_LOGOS_URL", f"{API_BASE}/logos.json")
BLOCKLIST_URL = os.getenv("IPTV_BLOCKLIST_URL", f"{API_BASE}/blocklist.json")

CONFIG = Path(os.getenv("CHANNEL_CONFIG", "channels.json"))
EXTRAS = Path(os.getenv("EXTRA_STREAMS", "extra_streams.json"))
FALLBACK_CONFIG = Path(os.getenv("FALLBACK_SOURCES", "fallback_sources.json"))
OUT_M3U = Path(os.getenv("OUT_M3U", "turkiye-tv.m3u"))
OUT_REPORT = Path(os.getenv("OUT_REPORT", "playlist-report.csv"))
USER_AGENT = os.getenv("PLAYLIST_USER_AGENT", "Mozilla/5.0 (Turkey-Curated-IPTV-Builder/3.4)")
HEALTHCHECK = os.getenv("STREAM_HEALTHCHECK", "1").strip().lower() not in {"0", "false", "no", "off"}
HEALTH_FAIL_OPEN = os.getenv("STREAM_HEALTH_FAIL_OPEN", "1").strip().lower() not in {"0", "false", "no", "off"}
HEALTH_TIMEOUT = float(os.getenv("STREAM_HEALTH_TIMEOUT", "10"))
RESOLVER_TIMEOUT = float(os.getenv("OFFICIAL_RESOLVER_TIMEOUT", "35"))
MAX_IPTV_ORG_CANDIDATES = int(os.getenv("MAX_IPTV_ORG_CANDIDATES", "3"))
MAX_MANIFEST_BYTES = 512 * 1024
PROBE_BYTES = 4096


def fetch_bytes(url: str, timeout=90, headers=None, max_bytes=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update({k: v for k, v in headers.items() if v})
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes) if max_bytes else r.read()


def fetch_json(url: str):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def norm(text: str) -> str:
    text = (text or "").casefold().replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(hd|sd|fhd|uhd|4k|720p|1080p|canli|live)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_attrs(extinf: str):
    return {k: v for k, v in re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def parse_m3u(text: str):
    lines = [x.rstrip("\r") for x in text.splitlines()]
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("#EXTINF:"):
            i += 1
            continue
        attrs = parse_attrs(lines[i])
        display = lines[i].split(",", 1)[1].strip() if "," in lines[i] else attrs.get("tvg-name", "")
        referrer = ""
        user_agent = ""
        url = ""
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt:
                j += 1
                continue
            if nxt.startswith("#EXTINF:"):
                break
            if nxt.startswith("#EXTVLCOPT:http-referrer="):
                referrer = nxt.split("=", 1)[1]
                j += 1
                continue
            if nxt.startswith("#EXTVLCOPT:http-user-agent="):
                user_agent = nxt.split("=", 1)[1]
                j += 1
                continue
            if nxt.startswith("#"):
                j += 1
                continue
            url = lines[j].strip()
            break
        out.append({
            "name": display,
            "tvg_name": attrs.get("tvg-name", ""),
            "tvg_id": attrs.get("tvg-id", ""),
            "logo": attrs.get("tvg-logo", ""),
            "url": url,
            "referrer": referrer,
            "user_agent": user_agent,
        })
        i = max(j + 1, i + 1)
    return out


def quality_value(q):
    if not q:
        return 0
    nums = [int(x) for x in re.findall(r"\d+", str(q))]
    return max(nums) if nums else 0


def stream_score(s, preferred_feeds=None):
    label = (s.get("label") or "").casefold()
    score = quality_value(s.get("quality")) * 10
    if not label:
        score += 4000
    if "geo" in label:
        score -= 2500
    if "not 24/7" in label or "offline" in label:
        score -= 5000
    if str(s.get("url", "")).startswith("https://"):
        score += 50
    if not s.get("referrer") and not s.get("user_agent"):
        score += 20
    preferred_feeds = [norm(x) for x in (preferred_feeds or [])]
    feed = norm(str(s.get("feed") or ""))
    if preferred_feeds:
        if feed in preferred_feeds:
            score += 100000
        elif feed:
            score -= 10000
    return score


def logo_score(l, feed=None):
    score = 0
    if l.get("in_use") is True:
        score += 1000
    if feed and l.get("feed") == feed:
        score += 300
    elif l.get("feed") is None:
        score += 150
    tags = [str(x).casefold() for x in (l.get("tags") or [])]
    if "horizontal" in tags:
        score += 100
    fmt = (l.get("format") or "").upper()
    if fmt in {"PNG", "WEBP", "JPEG", "JPG"}:
        score += 50
    score += min(int(l.get("width") or 0), 2000) / 100
    return score


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: cannot read {path}: {exc}")
        return default


def resolve_channel(entry, channels_by_id, all_channels):
    for cid in entry.get("ids", []):
        if cid in channels_by_id:
            return channels_by_id[cid], "id"
    wanted = {norm(entry.get("name", ""))}
    wanted.update(norm(x) for x in entry.get("aliases", []))
    wanted.discard("")
    candidates = []
    for ch in all_channels:
        names = {norm(ch.get("name", ""))}
        names.update(norm(x) for x in (ch.get("alt_names") or []))
        if wanted & names:
            candidates.append(ch)
    if candidates:
        candidates.sort(key=lambda c: (c.get("country") != "TR", bool(c.get("closed")), c.get("id", "")))
        return candidates[0], "name"
    return None, "missing"


def choose_logo(channel_id, logos_by_channel, feed=None):
    options = logos_by_channel.get(channel_id, [])
    if not options:
        return ""
    return max(options, key=lambda l: logo_score(l, feed)).get("url") or ""


def extinf(entry, channel_id, name, logo):
    num = str(entry.get("number", "")).strip()
    attrs = [
        f'tvg-id="{channel_id}"',
        f'tvg-name="{name}"',
        f'tvg-logo="{logo}"',
        f'tvg-chno="{num}"',
        'group-title="Türkiye TV"',
    ]
    return "#EXTINF:-1 " + " ".join(attrs) + "," + name


def fallback_keys(entry):
    vals = [entry.get("name", "")] + entry.get("aliases", []) + entry.get("fallback_names", [])
    return {norm(v) for v in vals if norm(v)}


def fallback_entry_keys(item):
    vals = [item.get("name", ""), item.get("tvg_name", ""), item.get("tvg_id", "")]
    return {norm(v) for v in vals if norm(v)}


def find_fallbacks(entry, fallback_catalog):
    wanted = fallback_keys(entry)
    if not wanted:
        return []
    return [(source_name, item) for source_name, item in fallback_catalog
            if item.get("url") and wanted & fallback_entry_keys(item)]


def load_fallback_catalog():
    cfg = load_json(FALLBACK_CONFIG, {})
    catalog = []
    errors = []
    for source in cfg.get("sources", []):
        if not source.get("enabled", True) or not source.get("url"):
            continue
        name = source.get("name") or source["url"]
        try:
            print(f"Downloading fallback M3U: {name}")
            text = fetch_bytes(source["url"]).decode("utf-8-sig", errors="replace")
            items = parse_m3u(text)
            catalog.extend((name, x) for x in items)
            print(f"  parsed entries: {len(items)}")
        except Exception as exc:
            errors.append((name, str(exc)))
            print(f"WARNING: fallback source failed: {name}: {exc}")
    return catalog, errors


def candidate_headers(candidate):
    headers = {"Accept": "*/*"}
    ua = candidate.get("user_agent") or USER_AGENT
    headers["User-Agent"] = ua
    if candidate.get("referrer"):
        headers["Referer"] = candidate["referrer"]
    return headers


def http_probe(url, headers, max_bytes=PROBE_BYTES, timeout=HEALTH_TIMEOUT):
    h = dict(headers)
    h.setdefault("Range", f"bytes=0-{max_bytes - 1}")
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", 200) or 200
        data = r.read(max_bytes)
        ctype = r.headers.get("Content-Type", "")
        final_url = r.geturl()
    if status < 200 or status >= 400:
        return False, f"http_{status}", data, ctype, final_url
    if not data:
        return False, "empty_response", data, ctype, final_url
    return True, f"http_{status}", data, ctype, final_url


def manifest_probe(url, headers, max_bytes=MAX_MANIFEST_BYTES, timeout=HEALTH_TIMEOUT):
    """Fetch a manifest without a Range header.

    A surprising number of HLS CDNs either reject Range requests for .m3u8 files or
    return a non-manifest response to them. V3.2 used the same range probe for
    manifests and segments, causing false negatives (notably valid MPEGURL MIME types).
    """
    h = dict(headers)
    h.pop("Range", None)
    h.setdefault("Accept-Encoding", "identity")
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", 200) or 200
        data = r.read(max_bytes)
        ctype = r.headers.get("Content-Type", "")
        final_url = r.geturl()
    if status < 200 or status >= 400:
        return False, f"http_{status}", data, ctype, final_url
    if not data:
        return False, "empty_response", data, ctype, final_url
    return True, f"http_{status}", data, ctype, final_url


def _attr_uri(line):
    m = re.search(r'URI="([^"]+)"', line)
    return m.group(1) if m else ""


def _master_variants(text):
    lines = [x.strip() for x in text.splitlines()]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        bw = 0
        m = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line)
        if m:
            bw = int(m.group(1))
        for j in range(i + 1, min(i + 5, len(lines))):
            if lines[j] and not lines[j].startswith("#"):
                variants.append((bw, lines[j]))
                break
    variants.sort(reverse=True)
    return [u for _, u in variants]


def _media_uris(text):
    lines = [x.strip() for x in text.splitlines()]
    key_uris = []
    map_uris = []
    media = []
    for line in lines:
        if line.startswith("#EXT-X-KEY:"):
            u = _attr_uri(line)
            if u and "METHOD=NONE" not in line:
                key_uris.append(u)
        elif line.startswith("#EXT-X-MAP:"):
            u = _attr_uri(line)
            if u:
                map_uris.append(u)
        elif line and not line.startswith("#"):
            media.append(line)
    return key_uris, map_uris, media


def probe_hls(url, headers, depth=0):
    if depth > 2:
        return False, "manifest_depth_exceeded"
    try:
        ok, detail, data, ctype, final_url = manifest_probe(url, headers, MAX_MANIFEST_BYTES)
    except HTTPError as exc:
        return False, f"http_{exc.code}"
    except (URLError, socket.timeout, TimeoutError) as exc:
        return False, f"network_{str(exc.reason) if isinstance(exc, URLError) else str(exc)}"
    except Exception as exc:
        return False, f"error_{type(exc).__name__}:{exc}"
    if not ok:
        return False, detail
    text = data.decode("utf-8-sig", errors="replace")
    low = (ctype or "").lower()
    hls_mime = any(x in low for x in (
        "application/vnd.apple.mpegurl", "application/x-mpegurl",
        "audio/mpegurl", "audio/x-mpegurl", "application/mpegurl"
    ))
    if "#EXTM3U" not in text[:8192]:
        # Some servers identify a valid HLS endpoint with the canonical MPEGURL MIME
        # but return an unusual body to automated probes. Do not incorrectly delete
        # such a channel; TiviMate/players can still parse the endpoint themselves.
        if hls_mime and (url.lower().split("?", 1)[0].endswith(".m3u8") or "mpegurl" in low):
            return True, f"hls_mime_reachable:{ctype}"
        # Some valid direct media URLs are not HLS; keep them only if content type looks media-like.
        if any(x in low for x in ("video/", "audio/", "application/octet-stream")):
            return True, f"direct_media:{detail}"
        return False, f"not_m3u8:{ctype or 'unknown_content_type'}"

    variants = _master_variants(text)
    if variants:
        failures = []
        for rel in variants[:3]:
            child = urljoin(final_url, rel)
            ok2, det2 = probe_hls(child, headers, depth + 1)
            if ok2:
                return True, f"master_ok->{det2}"
            failures.append(det2)
        return False, "master_variants_failed:" + "|".join(failures[:3])

    key_uris, map_uris, media = _media_uris(text)
    # Verify key/init objects when present, because a manifest can be 200 while playback still 403s.
    for rel in key_uris[:1] + map_uris[:1]:
        target = urljoin(final_url, rel)
        try:
            ok2, det2, *_ = http_probe(target, headers, PROBE_BYTES)
        except HTTPError as exc:
            return False, f"dependency_http_{exc.code}"
        except Exception as exc:
            return False, f"dependency_error:{type(exc).__name__}:{exc}"
        if not ok2:
            return False, f"dependency_failed:{det2}"

    if not media:
        return False, "no_media_segment"
    # Test up to two segments; live manifests can contain one stale segment during rotation.
    failures = []
    for rel in media[:2]:
        segment = urljoin(final_url, rel)
        try:
            ok2, det2, segdata, *_ = http_probe(segment, headers, PROBE_BYTES)
        except HTTPError as exc:
            failures.append(f"http_{exc.code}")
            continue
        except Exception as exc:
            failures.append(f"{type(exc).__name__}:{exc}")
            continue
        if ok2 and segdata:
            return True, f"hls_segment_ok:{det2}"
        failures.append(det2)
    return False, "segments_failed:" + "|".join(failures)


def probe_candidate(candidate):
    if not HEALTHCHECK:
        return True, "healthcheck_disabled"
    url = candidate.get("url", "")
    if not url:
        return False, "missing_url"
    headers = candidate_headers(candidate)
    return probe_hls(url, headers)



def ambiguous_runner_failure(detail: str) -> bool:
    """Failures that can be caused by GitHub runner geography/CDN policy.

    These should be reported but, by default, must not remove an otherwise selected
    public stream because the user's TV is in Turkey while the hosted runner may not be.
    """
    d = (detail or "").casefold()
    markers = [
        "http_401", "http_403", "http_451",
        "network_", "timeout", "timed out", "name or service not known",
        "temporary failure", "connection reset", "connection refused",
        "remotedisconnected", "remote end closed connection", "connection aborted",
        "ssl", "certificate",
    ]
    return any(m in d for m in markers)

def _decode_htmlish(text: str) -> str:
    text = html.unescape(text or "")
    # Common JSON/JS escaping used by embedded player configs.
    text = text.replace(r"\/", "/").replace(r"\u0026", "&").replace(r"\u003d", "=")
    return text


def _fetch_page_impersonated(page_url: str):
    """Fetch an official live page using a browser TLS fingerprint when available."""
    try:
        from curl_cffi import requests as curl_requests  # installed by the workflow
        r = curl_requests.get(
            page_url,
            impersonate="chrome",
            timeout=RESOLVER_TIMEOUT,
            headers={"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7"},
        )
        if 200 <= r.status_code < 400:
            return r.text, f"curl_cffi_http_{r.status_code}"
        return "", f"curl_cffi_http_{r.status_code}"
    except Exception as exc:
        try:
            data = fetch_bytes(
                page_url,
                timeout=RESOLVER_TIMEOUT,
                headers={"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7"},
                max_bytes=2 * 1024 * 1024,
            )
            return data.decode("utf-8", errors="replace"), "urllib_ok"
        except Exception as exc2:
            return "", f"page_fetch_failed:{type(exc).__name__}:{exc}|{type(exc2).__name__}:{exc2}"


def _extract_embedded_urls(page_text: str):
    text = _decode_htmlish(page_text)
    m3u8 = []
    youtube = []
    # Keep URL matching conservative: stop at quotes, whitespace and common HTML delimiters.
    for u in re.findall(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', text, flags=re.I):
        u = u.rstrip('),;]')
        if u not in m3u8:
            m3u8.append(u)
    yt_patterns = [
        r'https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{6,})',
        r'https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{6,})',
        r'https?://youtu\.be/([A-Za-z0-9_-]{6,})',
    ]
    for pat in yt_patterns:
        for vid in re.findall(pat, text, flags=re.I):
            u = f"https://www.youtube.com/watch?v={vid}"
            if u not in youtube:
                youtube.append(u)
    return m3u8, youtube


def _yt_dlp_resolve(target_url: str):
    cmd = [
        "yt-dlp", "--no-warnings", "--no-playlist", "--dump-single-json",
        "--impersonate", "chrome", "--js-runtimes", "deno",
        "--remote-components", "ejs:npm",
        "-f", "best[protocol^=m3u8]/best", target_url,
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=RESOLVER_TIMEOUT, check=False)
    except FileNotFoundError:
        return None, "yt_dlp_not_installed"
    except subprocess.TimeoutExpired:
        return None, "yt_dlp_timeout"
    except Exception as exc:
        return None, f"yt_dlp_error:{type(exc).__name__}:{exc}"
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "yt-dlp failed").strip().replace("\n", " ")
        return None, "yt_dlp_failed:" + err[:260]
    try:
        data = json.loads(cp.stdout)
    except Exception as exc:
        return None, f"yt_dlp_json_error:{type(exc).__name__}:{exc}"
    if not data.get("url") and data.get("entries"):
        data = next((x for x in data.get("entries") or [] if isinstance(x, dict) and x.get("url")), data)
    url = data.get("url") or ""
    if not url:
        return None, "yt_dlp_no_url"
    hh = data.get("http_headers") or {}
    return {
        "url": url,
        "referrer": hh.get("Referer") or hh.get("referer") or "",
        "user_agent": hh.get("User-Agent") or hh.get("user-agent") or "",
        "title": data.get("title") or "",
        "extractor": data.get("extractor_key") or data.get("extractor") or "yt-dlp",
    }, "ok"


def resolve_official_page(page_url: str):
    """Resolve explicitly configured public/official pages.

    V3.4 first parses the page itself for direct HLS or an embedded YouTube ID.
    This handles sites that yt-dlp labels "Unsupported URL" even though the page
    contains a perfectly ordinary iframe/player. yt-dlp is then used only where it
    is useful, with browser impersonation + Deno/EJS enabled by the workflow.
    """
    if not page_url:
        return [], ["missing_page_url"]
    candidates = []
    failures = []

    page_text, fetch_detail = _fetch_page_impersonated(page_url)
    if page_text:
        hls_urls, youtube_urls = _extract_embedded_urls(page_text)
        for u in hls_urls[:8]:
            candidates.append({
                "url": u,
                "referrer": page_url,
                "user_agent": USER_AGENT,
                "title": "embedded HLS",
                "extractor": "html",
            })
        for ytu in youtube_urls[:4]:
            resolved, detail = _yt_dlp_resolve(ytu)
            if resolved:
                candidates.append(resolved)
            else:
                failures.append(f"youtube:{ytu}:{detail}")
    else:
        failures.append(fetch_detail)

    # Also try yt-dlp on the page itself; supported sites may resolve signed URLs.
    resolved, detail = _yt_dlp_resolve(page_url)
    if resolved:
        if not any(x.get("url") == resolved.get("url") for x in candidates):
            candidates.append(resolved)
    else:
        failures.append(f"page:{detail}")
    return candidates, failures


def make_candidate(source, source_detail, url, **kwargs):
    d = {
        "source": source,
        "source_detail": source_detail,
        "url": url,
        "referrer": kwargs.get("referrer", "") or "",
        "user_agent": kwargs.get("user_agent", "") or "",
        "logo": kwargs.get("logo", "") or "",
        "tvg_id": kwargs.get("tvg_id", "") or "",
        "name": kwargs.get("name", "") or "",
        "quality": kwargs.get("quality", "") or "",
        "label": kwargs.get("label", "") or "",
        "stream_title": kwargs.get("stream_title", "") or "",
        "feed": kwargs.get("feed"),
    }
    return d


def main():
    config = load_json(CONFIG, {})
    wanted = config.get("channels", config if isinstance(config, list) else [])
    extras = load_json(EXTRAS, {})

    print("Downloading iptv-org API data...")
    channels = fetch_json(CHANNELS_URL)
    streams = fetch_json(STREAMS_URL)
    logos = fetch_json(LOGOS_URL)
    blocklist = fetch_json(BLOCKLIST_URL)
    fallback_catalog, fallback_errors = load_fallback_catalog()

    channels_by_id = {c.get("id"): c for c in channels if c.get("id")}
    streams_by_channel = {}
    for s in streams:
        cid = s.get("channel")
        if cid:
            streams_by_channel.setdefault(cid, []).append(s)
    logos_by_channel = {}
    for l in logos:
        cid = l.get("channel")
        if cid:
            logos_by_channel.setdefault(cid, []).append(l)
    blocked = {b.get("channel"): b for b in blocklist if b.get("channel")}

    lines = ["#EXTM3U"]
    rows = []
    added = 0

    for position, entry in enumerate(wanted, start=1):
        configured_id = (entry.get("ids") or [""])[0]
        extra_items = []
        for key in [configured_id, entry.get("name", "")]:
            raw_extra = extras.get(key) if key else None
            if not raw_extra:
                continue
            if isinstance(raw_extra, list):
                extra_items.extend(x for x in raw_extra if isinstance(x, dict) and x.get("enabled", True))
            elif isinstance(raw_extra, dict) and raw_extra.get("enabled", True):
                extra_items.append(raw_extra)
            if extra_items:
                break

        channel, resolution = resolve_channel(entry, channels_by_id, channels)
        resolved_id = channel.get("id") if channel else configured_id
        official_page = entry.get("official_page", "")
        block = blocked.get(resolved_id)
        display_name = entry.get("name") or (channel or {}).get("name") or resolved_id
        candidates = []
        denied_candidates = []
        deny_contains = [str(x) for x in entry.get("deny_url_contains", []) if str(x)]

        def allowed_url(url):
            if any(part in (url or "") for part in deny_contains):
                denied_candidates.append(url or "")
                return False
            return True

        # 1) iptv-org candidates, best first. Skip entirely when the channel is blocklisted.
        if channel and not block:
            options = sorted(streams_by_channel.get(resolved_id, []),
                             key=lambda x: stream_score(x, entry.get("preferred_feeds")), reverse=True)
            for s in options[:MAX_IPTV_ORG_CANDIDATES]:
                if not allowed_url(s.get("url", "")):
                    continue
                candidates.append(make_candidate(
                    "iptv-org", resolved_id, s.get("url", ""),
                    referrer=s.get("referrer"), user_agent=s.get("user_agent"),
                    logo=choose_logo(resolved_id, logos_by_channel, s.get("feed")),
                    tvg_id=resolved_id, name=display_name, quality=s.get("quality"),
                    label=s.get("label"), stream_title=s.get("title"), feed=s.get("feed")
                ))

        # 2) Curated fallbacks. V3.4 allows multiple candidates per channel;
        #    health checks select the first one that actually works.
        for extra in extra_items:
            urls = extra.get("urls") or ([extra.get("url")] if extra.get("url") else [])
            for extra_url in urls:
                if not extra_url or not allowed_url(extra_url):
                    continue
                candidates.append(make_candidate(
                    "extra_streams.json", extra.get("note") or "curated fallback", extra_url,
                    referrer=extra.get("referrer"), user_agent=extra.get("user_agent"), logo=extra.get("logo"),
                    tvg_id=extra.get("tvg_id") or resolved_id or configured_id or f"custom.{norm(display_name)}",
                    name=extra.get("name") or display_name, stream_title=extra.get("name") or display_name
                ))

        # 3) Resolve explicitly configured official/public live pages (YouTube or
        #    native public web players). This is a last-resort source and is refreshed
        #    by the scheduled workflow because signed URLs can expire.
        resolver_failures = []
        for page_url in entry.get("resolver_urls", []):
            resolved_list, resolver_errors = resolve_official_page(page_url)
            resolver_failures.extend(f"{page_url}:{x}" for x in resolver_errors)
            for resolved in resolved_list:
                if not allowed_url(resolved.get("url", "")):
                    continue
                candidates.append(make_candidate(
                    "official_resolver", page_url, resolved.get("url", ""),
                    referrer=resolved.get("referrer"), user_agent=resolved.get("user_agent"),
                    tvg_id=resolved_id or configured_id or f"custom.{norm(display_name)}",
                    name=display_name, stream_title=resolved.get("title") or display_name
                ))

        # 4) Optional remote fallback M3Us.
        for fb_source, item in find_fallbacks(entry, fallback_catalog):
            if not allowed_url(item.get("url", "")):
                continue
            candidates.append(make_candidate(
                "fallback_m3u", fb_source, item.get("url", ""),
                referrer=item.get("referrer"), user_agent=item.get("user_agent"), logo=item.get("logo"),
                tvg_id=item.get("tvg_id") or resolved_id or configured_id or f"custom.{norm(display_name)}",
                name=display_name, stream_title=item.get("name") or display_name
            ))

        selected = None
        selected_unverified = False
        ambiguous_candidate = None
        ambiguous_detail = ""
        failed = []
        for idx, cand in enumerate(candidates, start=1):
            ok, detail = probe_candidate(cand)
            if ok:
                selected = cand
                health_status = "pass"
                health_detail = detail
                break
            failed.append(f"{cand['source']}#{idx}:{detail}")
            if HEALTH_FAIL_OPEN and ambiguous_candidate is None and ambiguous_runner_failure(detail):
                ambiguous_candidate = cand
                ambiguous_detail = detail

        # If every candidate failed only in an environment-dependent way (for example
        # a 403 from a non-Turkey GitHub runner), preserve the highest-priority such
        # candidate instead of creating a false negative for a viewer in Turkey.
        if selected is None and ambiguous_candidate is not None:
            selected = ambiguous_candidate
            selected_unverified = True
            health_status = "unverified_fail_open"
            health_detail = ambiguous_detail

        if selected:
            final_id = selected.get("tvg_id") or resolved_id or configured_id or f"custom.{norm(display_name)}"
            final_name = selected.get("name") or display_name
            logo = selected.get("logo") or (choose_logo(channel.get("id"), logos_by_channel) if channel else "")
            lines.append(extinf(entry, final_id, final_name, logo))
            if selected.get("referrer"):
                lines.append(f"#EXTVLCOPT:http-referrer={selected['referrer']}")
            if selected.get("user_agent"):
                lines.append(f"#EXTVLCOPT:http-user-agent={selected['user_agent']}")
            lines.append(selected["url"])
            added += 1
            suffix = "unverified" if selected_unverified else "healthy"
            if selected["source"] == "iptv-org":
                status = f"added_iptv_org_{suffix}"
            elif selected["source"] == "extra_streams.json":
                status = f"added_extra_fallback_{suffix}"
            elif selected["source"] == "official_resolver":
                status = f"added_official_resolver_{suffix}"
            else:
                status = f"added_fallback_{suffix}"
            source = selected["source"]
            source_detail = selected["source_detail"]
            quality = selected.get("quality", "")
            label = selected.get("label", "")
            stream_title = selected.get("stream_title", "")
            health_url = selected.get("url", "")
            resolved_id = final_id
        else:
            source = ""
            source_detail = ""
            quality = ""
            label = ""
            stream_title = ""
            health_status = "fail" if candidates else "not_tested_no_candidate"
            if failed:
                health_detail = " || ".join(failed)
                if resolver_failures:
                    health_detail += " || resolver:" + " || ".join(resolver_failures)
            elif resolver_failures:
                health_detail = "resolver:" + " || ".join(resolver_failures)
            elif denied_candidates:
                health_detail = "known_bad_url_skipped:" + "|".join(denied_candidates)
            else:
                health_detail = "no_candidate_url"
            health_url = ""
            if candidates:
                status = "all_candidates_failed_healthcheck"
            elif denied_candidates:
                status = "known_bad_stream_no_fallback"
            elif block:
                status = "blocked_no_fallback"
            elif channel:
                status = "no_public_stream_no_fallback"
            else:
                status = "channel_not_found_no_fallback"

        rows.append({
            "playlist_position": position,
            "digiturk_number": entry.get("number", ""),
            "wanted_name": entry.get("name", ""),
            "resolved_id": resolved_id or "",
            "resolution": resolution,
            "status": status,
            "source": source,
            "source_detail": source_detail,
            "health_status": health_status,
            "health_detail": health_detail,
            "health_url": health_url,
            "candidate_count": len(candidates),
            "quality": quality,
            "label": label,
            "stream_title": stream_title,
            "block_reason": (block or {}).get("reason", "") if block else "",
            "official_page": official_page,
        })

    OUT_M3U.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fields = [
        "playlist_position", "digiturk_number", "wanted_name", "resolved_id", "resolution", "status",
        "source", "source_detail", "health_status", "health_detail", "health_url", "candidate_count",
        "quality", "label", "stream_title", "block_reason", "official_page"
    ]
    with OUT_REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Configured channels: {len(wanted)}")
    print(f"Health check: {'ON' if HEALTHCHECK else 'OFF'} (timeout={HEALTH_TIMEOUT:g}s, fail_open_geo/network={HEALTH_FAIL_OPEN})")
    print(f"Added to playlist after health checks: {added}")
    print(f"Wrote: {OUT_M3U} and {OUT_REPORT}")
    missing = [r for r in rows if not r["status"].startswith("added_")]
    if missing:
        print("Unavailable channels after all sources + health checks:")
        for r in missing:
            detail = r["health_detail"]
            if len(detail) > 220:
                detail = detail[:217] + "..."
            print(f"  pos {r['playlist_position']:>2} / ch {str(r['digiturk_number']):>3} {r['wanted_name']}: {r['status']} | {detail}")
    if fallback_errors:
        print("Fallback source errors (build continued):")
        for name, err in fallback_errors:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
