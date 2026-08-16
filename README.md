# Türkiye TV V3.3

This version fixes V3.2 health-check false negatives and adds an optional dynamic resolver for explicitly configured **official/public** live pages. It never resolves subscription/paywalled channels.

Key changes:
- HLS manifests are fetched without Range headers.
- `application/vnd.apple.mpegurl` and `application/x-mpegURL` are recognized as valid HLS MIME types.
- GitHub-runner `RemoteDisconnected` errors are treated as geography/network-ambiguous instead of definite dead streams.
- Selected free official live pages can be resolved with `yt-dlp` as a last resort.
- Workflow refreshes every 4 hours because signed live URLs may expire.
- GitHub Actions upgraded to Node-24 based action versions (`checkout@v6`, `setup-python@v6`).

The TiviMate URLs remain unchanged:

```text
M3U: https://raw.githubusercontent.com/YananHelena/iptv/main/turkiye-tv.m3u
EPG: https://raw.githubusercontent.com/YananHelena/iptv/main/turkiye-epg.xml.gz
```

Do not re-enable the previously user-tested-bad Show TV/TLC/DMAX static fallback URLs. 360's known black-screen URLs also remain denied.
