#!/usr/bin/env python3
"""Download card images once into out/img/ so the app never hotlinks upstream.

YGOPRODeck forbids hotlinking (download once, self-host); the other sources allow
hotlinking but mirroring keeps the app independent of their uptime.

Files are keyed by the SOURCE IMAGE URL, not by card id: one Yu-Gi-Oh! artwork is shared
by every printing of the card (measured: 1,050 printings -> 270 images), so keying by card
id stored the same picture ~4x. Mirrored images are re-encoded to WebP so the whole
Yu-Gi-Oh! catalog fits GitHub Pages (measured 2026-11-12 on 60 random cards: originals
813x1185 JPEG ~165 KB; WebP 420 px q65 ~37 KB -> ~550 MB for all 14,836 images).

    python3 mirror_images.py --game ygo --limit 20     # local smoke test
    python3 mirror_images.py --game ygo                # CI: everything not yet mirrored

Needs Pillow (`pip install pillow`) unless --keep-original.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from build import HOST_DELAY, UA

HERE = Path(__file__).resolve().parent
WIDTH = 420      # card detail shows the image at 132 pt -> 396 px @3x
QUALITY = 65     # WebP; text on the card stays legible (checked by eye on samples)


def encode(raw: bytes) -> bytes:
    from PIL import Image  # imported lazily so --keep-original works without Pillow
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    if im.width > WIDTH:
        im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=QUALITY, method=6)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--game", action="append")
    ap.add_argument("--limit", type=int, default=0, help="max NEW downloads this run")
    ap.add_argument("--keep-original", action="store_true", help="store upstream bytes as-is")
    a = ap.parse_args()

    images: dict[str, str] = json.loads((a.out / "images.json").read_text())
    manifest_path = a.out / "img" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    last: dict[str, float] = {}
    by_url: dict[str, str] = {}  # source URL -> rel path, filled as we go
    done = reused = failed = 0
    for card_id, url in images.items():
        game = card_id.split(":", 1)[0]
        if a.game and game not in a.game:
            continue
        ext = Path(urllib.parse.urlparse(url).path).suffix or ".img"
        if not a.keep_original:
            ext = ".webp"
        rel = f"{game}/{hashlib.sha1(url.encode()).hexdigest()[:16]}{ext}"
        dest = a.out / "img" / rel
        if dest.exists() or by_url.get(url) == rel:
            if manifest.get(card_id) != rel:
                manifest[card_id] = rel
                reused += 1
            continue
        if a.limit and done >= a.limit:
            continue  # keep walking so already-mirrored printings still get mapped
        host = urllib.parse.urlparse(url).hostname or ""
        wait = max(HOST_DELAY.get(host, 0.2), 0.2) - (time.time() - last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        last[host] = time.time()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            dest.write_bytes(raw if a.keep_original else encode(raw))
            by_url[url] = rel
            manifest[card_id] = rel
            done += 1
        except Exception as e:  # keep going; the next nightly run retries
            failed += 1
            print(f"  ! {card_id}: {e}")
    # drop entries whose file is gone (e.g. the old per-card-id layout)
    manifest = {k: v for k, v in manifest.items() if (a.out / "img" / v).exists()}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")))
    size = sum(p.stat().st_size for p in (a.out / "img").rglob("*") if p.is_file())
    print(f"downloaded {done}, mapped to existing {reused}, failed {failed}, "
          f"cards in manifest {len(manifest)}, img/ {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
