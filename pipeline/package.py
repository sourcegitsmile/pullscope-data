#!/usr/bin/env python3
"""Package build.py output (out/) into the CDN contract the app reads (PRD file 4 §2).

    python3 package.py                 # out/ -> dist/v1/...
    python3 package.py --out out --dist dist

Writes:
    dist/v1/manifest.json
    dist/v1/<game>/catalog-<version>.json.gz     version = sha1 of the cards, so unchanged data keeps its URL
    dist/v1/<game>/prices-<yyyy-MM-dd>.json.gz   one file per tcgcsv snapshot day

Game keys follow the app's `Game` enum (`ygo` -> `yugioh`). Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
MIRRORED = {"ygo"}  # dec_0013: only Yu-Gi-Oh! images are mirrored (YGOPRODeck forbids hotlinking)
GAME_KEY = {"pokemon": "pokemon", "mtg": "mtg", "ygo": "yugioh", "onepiece": "onepiece", "lorcana": "lorcana"}


def set_code(game: str, set_id: str) -> str:
    # One Piece sets are "OP-01" at the source but printed as "OP01-001" on the card.
    return set_id.replace("-", "") if game == "onepiece" else set_id


def write_gz(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    # mtime=0 so identical content gives identical bytes
    data = gzip.compress(raw, compresslevel=9, mtime=0)
    path.write_bytes(data)
    return len(data)


def package(out: Path, dist: Path, image_base: str | None = None) -> dict:
    """`image_base`: public base URL of the CDN. When given, mirrored games point `imageURL` at
    `{image_base}/img/<game>/<file>` and the files are copied to `dist/img/<game>/`. Without it the
    upstream URL is kept (local testing only — never ship YGO that way)."""
    meta = json.loads((out / "meta.json").read_text())
    mirror_path = out / "img" / "manifest.json"
    mirrored = json.loads(mirror_path.read_text()) if mirror_path.exists() else {}
    price_day = meta["prices"]["updated_at"][:10]
    manifest = {"schemaVersion": SCHEMA_VERSION, "generatedAt": meta["generated_at"],
                "priceSource": meta["prices"]["source"], "games": {}}
    for src_game, game in GAME_KEY.items():
        gdir = out / src_game
        if not (gdir / "sets.json").exists():
            continue
        released = {s["id"]: s.get("released") for s in json.loads((gdir / "sets.json").read_text())}
        cards, prices, seen_prices = [], [], set()
        for set_file in sorted(gdir.glob("*.json")):
            if set_file.name in ("sets.json", "index.json"):
                continue
            body = json.loads(set_file.read_text())
            for c in body["cards"]:
                image = c.get("image")
                rel = mirrored.get(c["id"]) if src_game in MIRRORED else None
                if image_base and rel:
                    file_name = rel.split("/", 1)[1]
                    target = dist / "img" / game / file_name
                    if not target.exists():  # many printings share one image
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(out / "img" / rel, target)
                    image = f"{image_base.rstrip('/')}/img/{game}/{file_name}"
                variants = [p["variant"] for p in c.get("prices", [])]
                cards.append({
                    "id": c["id"].replace(f"{src_game}:", f"{game}:", 1),
                    "name": c["name"], "setCode": set_code(src_game, c["set_id"]), "setName": c["set_name"],
                    "number": c["number"], "rarity": c.get("rarity"),
                    "passcode": str(c["passcode"]) if c.get("passcode") else None,
                    "imageURL": image, "tcgplayerProductId": c.get("tcgplayer_product_id"),
                    "variants": variants, "joinAmbiguous": bool(c.get("join_ambiguous")),
                    "releaseDate": released.get(c["set_id"]),
                })
                pid = c.get("tcgplayer_product_id")
                for p in c.get("prices", []):
                    key = (pid, p["variant"])
                    if pid is None or key in seen_prices:
                        continue
                    seen_prices.add(key)
                    prices.append({"productId": pid, "variant": p["variant"], "market": p.get("market"),
                                   "low": p.get("low"), "mid": p.get("mid"), "high": p.get("high")})
        cards.sort(key=lambda c: c["id"])
        dup = sorted({c["id"] for c in cards if sum(1 for x in cards if x["id"] == c["id"]) > 1}) if len(
            {c["id"] for c in cards}) != len(cards) else []
        if dup:  # the app stores id as unique: a duplicate would silently overwrite another printing
            raise SystemExit(f"{game}: duplicate card ids {dup[:5]}")
        version = hashlib.sha1(json.dumps(cards, sort_keys=True).encode()).hexdigest()[:12]
        cat_rel = f"v1/{game}/catalog-{version}.json.gz"
        price_rel = f"v1/{game}/prices-{price_day}.json.gz"
        cat_bytes = write_gz(dist / cat_rel, {"game": game, "version": version, "cards": cards})
        price_bytes = write_gz(dist / price_rel, {"game": game, "date": price_day, "prices": prices})
        stats = meta["games"].get(src_game, {})
        rate = (stats.get("with_market_price", 0) / stats["cards"]) if stats.get("cards") else None
        manifest["games"][game] = {
            "catalogVersion": version, "catalogURL": cat_rel, "catalogBytes": cat_bytes, "cardCount": len(cards),
            "priceDate": price_day, "pricesURL": price_rel, "pricesBytes": price_bytes,
            "priceMatchRate": round(rate, 4) if rate is not None else None,
        }
    (dist / "v1").mkdir(parents=True, exist_ok=True)
    (dist / "v1/manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--dist", type=Path, default=HERE / "dist")
    ap.add_argument("--image-base", help="public CDN base URL; rewrites mirrored image URLs to it")
    a = ap.parse_args()
    m = package(a.out, a.dist, a.image_base)
    for g, e in m["games"].items():
        print(f"{g:9} {e['cardCount']:6} cards  catalog {e['catalogBytes']/1024:7.1f} KB  "
              f"prices {e['pricesBytes']/1024:6.1f} KB  market {e['priceMatchRate']}")


if __name__ == "__main__":
    main()
