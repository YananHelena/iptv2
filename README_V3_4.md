# V3.4 changes

- Keeps the V3.3 stream health checks.
- Supports multiple curated fallback URLs per channel and tests them in order.
- Parses official live pages for embedded `.m3u8` URLs and YouTube embeds before calling yt-dlp.
- Uses `curl_cffi` browser impersonation for pages protected by TLS/browser fingerprinting.
- Adds Deno + yt-dlp EJS support in GitHub Actions for current YouTube extraction requirements.
- Adds health-checked candidates for A Para, Flash Haber, Bengü Türk, Ekotürk, Sözcü TV, KRT, A News and Yaban TV.
- Does **not** re-enable the user-confirmed bad Show TV/TLC/DMAX fallbacks or the black-screen 360 URLs.
- Does **not** force beIN Sports Haber or Spacetoon when no reliable public stream can be resolved.
