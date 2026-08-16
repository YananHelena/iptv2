#!/usr/bin/env python3
import csv
import gzip
import io
import re
import sys
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

PLAYLIST_PATH = Path("turkiye-tv.m3u")

EPG_SOURCES = [
    ("epgshare-tr1", "https://epgshare01.online/epgshare01/epg_ripper_TR1.xml.gz"),
    ("epgshare-tr3", "https://epgshare01.online/epgshare01/epg_ripper_TR3.xml.gz"),
    ("globetv-1", "https://raw.githubusercontent.com/globetvapp/epg/main/Turkey/turkey1.xml"),
    ("globetv-2", "https://raw.githubusercontent.com/globetvapp/epg/main/Turkey/turkey2.xml"),
    ("globetv-3", "https://raw.githubusercontent.com/globetvapp/epg/main/Turkey/turkey3.xml"),
    ("globetv-4", "https://raw.githubusercontent.com/globetvapp/epg/main/Turkey/turkey4.xml"),
    ("globetv-5", "https://raw.githubusercontent.com/globetvapp/epg/main/Turkey/turkey5.xml"),
]

OUT_XML = Path("turkiye-epg.xml")
OUT_GZ = Path("turkiye-epg.xml.gz")
OUT_REPORT = Path("match-report.csv")

USER_AGENT = "Mozilla/5.0 (Turkey-EPG-Builder/3.0)"

# Major known naming variants. Keys and values are normalized with norm().
ALIASES = {
    "nowtv": ["now", "nowtv", "foxtv"],
    "haberturktv": ["haberturk", "haberturktv", "haberturkhd"],
    "trt1": ["trt1", "trt1hd"],
    "trt2": ["trt2", "trt2hd"],
    "trthaber": ["trthaber", "trthaberhd"],
    "trtspor": ["trtspor", "trtsporhd"],
    "trtsporyildiz": ["trtsporyildiz", "trtyildiz", "trtspor2"],
    "kanald": ["kanald", "kanaldhd"],
    "showtv": ["show", "showtv", "showtvhd"],
    "startv": ["star", "startv", "starhd"],
    "tv8": ["tv8", "tv8hd"],
    "atv": ["atv", "atvhd"],
    "ahaber": ["ahaber", "ahaberhd"],
    "aspor": ["aspor", "asporhd"],
    "ntv": ["ntv", "ntvhd"],
    "cnnturk": ["cnnturk", "cnnturkhd"],
    "bloomberght": ["bloomberght", "bloomberght"],
    "kanal7": ["kanal7", "kanal7hd"],
    "tv100": ["tv100"],
    "haberglobal": ["haberglobal", "globalhaber"],
    "halktv": ["halktv"],
    "tele1": ["tele1"],
    "tlc": ["tlc"],
    "dmax": ["dmax"],
    "trtbelgesel": ["trtbelgesel"],
    "trtcocuk": ["trtcocuk"],
    "trtarabi": ["trtarabi", "trtarabic"],
    "trtworld": ["trtworld"],
    "ekolsports": ["ekolsports", "ekolspor"],
    "tv4": ["tv4", "tv 4"],
    "beinsportshaber": ["beinsportshaber", "beinsporhaber"],
    "euronewsenglish": ["euronews", "euronewsenglish"],
    "anews": ["anews", "a news"],
    "aljazeera": ["aljazeeraenglish", "aljazeerainternational", "aljazeera"],
    "nhkworldjapan": ["nhkworldjapan", "nhkworld"],
    "trteba": ["trteba", "trtebaortaokul"],
    "spacetoonturkey": ["spacetoonturkey", "spacetoon", "stoontv"],
    "tgrtbelgesel": ["tgrtbelgesel", "tgrtbelgeseltv"],
    "cgtndocumentary": ["cgtndocumentary"],
    "guneydogutv": ["guneydogutv", "guneydogu"],
}


def fetch(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def maybe_decompress(data: bytes, url: str) -> bytes:
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def norm(text: str) -> str:
    text = text or ""
    text = text.casefold()
    text = text.replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Remove common quality and broadcast noise.
    text = re.sub(r"@(sd|hd|fhd|uhd|4k|720p|1080p|50fps).*$", "", text)
    text = re.sub(r"\b(sd|hd|fhd|uhd|4k|720p|1080p|canli|live|turkiye|turkey)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def strip_quality_id(tvg_id: str) -> str:
    return re.sub(r"@[^@]+$", "", tvg_id or "")


def parse_attrs(extinf: str) -> dict:
    return {k: v for k, v in re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def slug_id(name: str, n: int) -> str:
    s = norm(name) or f"channel{n}"
    return f"custom.{s}.{n}"


def parse_playlist(text: str):
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    channels = []
    i = 0
    serial = 1
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            extinf = lines[i]
            attrs = parse_attrs(extinf)
            name = extinf.split(",", 1)[1].strip() if "," in extinf else attrs.get("tvg-name", "")
            url = ""
            j = i + 1
            extra_lines = []
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                if nxt.startswith("#EXTINF:"):
                    break
                if nxt.startswith("#"):
                    extra_lines.append(lines[j])
                    j += 1
                    continue
                url = lines[j]
                j += 1
                break
            tvg_id = attrs.get("tvg-id", "").strip()
            generated = False
            if not tvg_id:
                tvg_id = slug_id(name, serial)
                generated = True
            channels.append({
                "order": serial,
                "name": name,
                "tvg_id": tvg_id,
                "original_tvg_id": attrs.get("tvg-id", "").strip(),
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "logo": attrs.get("tvg-logo", "").strip(),
                "group": attrs.get("group-title", "").strip(),
                "extinf": extinf,
                "extra_lines": extra_lines,
                "url": url,
                "generated_id": generated,
            })
            serial += 1
            i = max(j, i + 1)
        else:
            i += 1
    return channels


def rewrite_extinf_tvg_id(extinf: str, tvg_id: str) -> str:
    if re.search(r'tvg-id="[^"]*"', extinf):
        return re.sub(r'tvg-id="[^"]*"', f'tvg-id="{tvg_id}"', extinf, count=1)
    if extinf.startswith("#EXTINF:"):
        head, rest = (extinf.split(",", 1) + [""])[:2]
        return f'{head} tvg-id="{tvg_id}",{rest}'
    return extinf


def parse_xmltv(data: bytes, source_name: str):
    root = ET.fromstring(data)
    programmes_by_id = {}
    channels = []

    for ch in root.findall("channel"):
        cid = ch.get("id", "")
        display_names = [(dn.text or "").strip() for dn in ch.findall("display-name") if (dn.text or "").strip()]
        channels.append({
            "source": source_name,
            "id": cid,
            "elem": ch,
            "names": display_names,
        })

    for pr in root.findall("programme"):
        cid = pr.get("channel", "")
        programmes_by_id.setdefault(cid, []).append(pr)

    return channels, programmes_by_id


def target_keys(ch):
    values = [ch["tvg_id"], strip_quality_id(ch["tvg_id"]), ch["name"], ch["tvg_name"]]
    keys = []
    for v in values:
        nv = norm(v)
        if nv and nv not in keys:
            keys.append(nv)
    expanded = list(keys)
    for key in keys:
        for alias_key, alias_values in ALIASES.items():
            allv = [alias_key] + alias_values
            if key in allv:
                expanded.extend(v for v in allv if v not in expanded)
    return expanded


def source_keys(src_ch):
    vals = [src_ch["id"], strip_quality_id(src_ch["id"])] + src_ch["names"]
    keys = []
    for v in vals:
        nv = norm(v)
        if nv and nv not in keys:
            keys.append(nv)
    return keys


def match_score(target, source):
    tkeys = target_keys(target)
    skeys = source_keys(source)
    if not tkeys or not skeys:
        return 0.0

    # Exact normalized matches get strongest priority.
    if set(tkeys) & set(skeys):
        return 1.0

    # If one normalized value fully contains the other, give a strong but not perfect score.
    best = 0.0
    for t in tkeys:
        for s in skeys:
            if len(t) >= 4 and len(s) >= 4 and (t in s or s in t):
                best = max(best, 0.93)
            best = max(best, SequenceMatcher(None, t, s).ratio())
    return best


def clone_channel_for_target(source_elem, target):
    ch = deepcopy(source_elem) if source_elem is not None else ET.Element("channel")
    ch.set("id", target["tvg_id"])
    if not ch.findall("display-name"):
        dn = ET.SubElement(ch, "display-name")
        dn.text = target["name"]
    else:
        # Put playlist name first so guide looks familiar in editors.
        first = ch.findall("display-name")[0]
        if (first.text or "").strip() != target["name"]:
            dn = ET.Element("display-name")
            dn.text = target["name"]
            ch.insert(0, dn)
    if target["logo"] and ch.find("icon") is None:
        icon = ET.SubElement(ch, "icon")
        icon.set("src", target["logo"])
    return ch


def main():
    if not PLAYLIST_PATH.exists():
        raise FileNotFoundError(f"Curated playlist not found: {PLAYLIST_PATH}. Run build_playlist.py first.")

    print(f"Reading curated playlist: {PLAYLIST_PATH}")
    playlist_text = PLAYLIST_PATH.read_text(encoding="utf-8-sig", errors="replace")
    playlist_channels = parse_playlist(playlist_text)
    print(f"EPG target entries: {len(playlist_channels)}")

    all_source_channels = []
    programme_lookup = {}
    source_errors = []

    for source_name, url in EPG_SOURCES:
        try:
            print(f"Downloading EPG source: {source_name}")
            raw = maybe_decompress(fetch(url), url)
            src_channels, src_programmes = parse_xmltv(raw, source_name)
            for sc in src_channels:
                sc["programme_count"] = len(src_programmes.get(sc["id"], []))
                all_source_channels.append(sc)
                programme_lookup[(source_name, sc["id"])] = src_programmes.get(sc["id"], [])
            print(f"  channels: {len(src_channels)}")
        except Exception as exc:
            source_errors.append((source_name, url, repr(exc)))
            print(f"WARNING: {source_name} failed: {exc}", file=sys.stderr)

    if not all_source_channels:
        raise RuntimeError("No EPG source could be downloaded/parsed.")

    out_root = ET.Element("tv", {
        "generator-info-name": "Turkey TV V3 EPG Builder",
        "generator-info-url": "https://github.com/",
    })

    report_rows = []
    matched = 0
    total_programmes = 0
    seen_programmes = set()

    for sc in all_source_channels:
        sc["_keys"] = source_keys(sc)

    for target in playlist_channels:
        candidates = []
        for sc in all_source_channels:
            score = match_score(target, sc)
            if score >= 0.84:
                candidates.append((score, sc.get("programme_count", 0), sc))

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        chosen = candidates[0][2] if candidates and candidates[0][0] >= 0.88 else None
        chosen_score = candidates[0][0] if chosen else 0.0

        if chosen:
            matched += 1
            out_root.append(clone_channel_for_target(chosen["elem"], target))
            programmes = programme_lookup.get((chosen["source"], chosen["id"]), [])
            copied = 0
            for pr in programmes:
                cp = deepcopy(pr)
                cp.set("channel", target["tvg_id"])
                title = cp.findtext("title") or ""
                key = (target["tvg_id"], cp.get("start", ""), cp.get("stop", ""), title)
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                out_root.append(cp)
                copied += 1
                total_programmes += 1
            report_rows.append({
                "playlist_name": target["name"],
                "playlist_tvg_id": target["tvg_id"],
                "original_tvg_id": target["original_tvg_id"],
                "matched": "yes",
                "source": chosen["source"],
                "source_channel_id": chosen["id"],
                "source_display_name": " | ".join(chosen["names"][:3]),
                "score": f"{chosen_score:.3f}",
                "programmes": copied,
                "generated_playlist_id": "yes" if target["generated_id"] else "no",
            })
        else:
            out_root.append(clone_channel_for_target(None, target))
            report_rows.append({
                "playlist_name": target["name"],
                "playlist_tvg_id": target["tvg_id"],
                "original_tvg_id": target["original_tvg_id"],
                "matched": "no",
                "source": "",
                "source_channel_id": "",
                "source_display_name": "",
                "score": f"{chosen_score:.3f}",
                "programmes": 0,
                "generated_playlist_id": "yes" if target["generated_id"] else "no",
            })

    try:
        ET.indent(out_root, space="  ")
    except AttributeError:
        pass
    xml_bytes = ET.tostring(out_root, encoding="utf-8", xml_declaration=True)
    OUT_XML.write_bytes(xml_bytes)
    with gzip.open(OUT_GZ, "wb", compresslevel=9) as f:
        f.write(xml_bytes)

    fieldnames = [
        "playlist_name", "playlist_tvg_id", "original_tvg_id", "matched", "source",
        "source_channel_id", "source_display_name", "score", "programmes", "generated_playlist_id"
    ]
    with OUT_REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(report_rows)

    print(f"Matched channels: {matched}/{len(playlist_channels)}")
    print(f"Programme rows copied: {total_programmes}")
    print(f"Wrote: {OUT_XML}, {OUT_GZ}, {OUT_REPORT}")
    if source_errors:
        print("Sources with errors:")
        for name, url, err in source_errors:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
