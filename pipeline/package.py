#!/usr/bin/env python3
"""Package build.py output (out/) into the CDN contract the app reads (PRD file 4 §2).

    python3 package.py                 # out/ -> dist/v1/...
    python3 package.py --out out --dist dist

Writes:
    dist/v1/manifest.json
    dist/v1/<game>/catalog-<version>.json.gz     version = sha1 of the cards, so unchanged data keeps its URL
    dist/v1/<game>/prices-<yyyy-MM-dd>.json.gz   one file per tcgcsv snapshot day
    dist/v1/<game>/sets.json                     set list (PRD 2.0, W2 S12/S13)
    dist/v1/history/<game>/<bucket>.json.gz      market price history, bucket = productId % HISTORY_BUCKETS
    dist/v1/history/meta.json                    first / last day the pipeline recorded
    dist/v1/trending-<yyyy-MM-dd>.json           day-over-day movers per game
    dist/v1/fx-<yyyy-MM-dd>.json                 ECB rates re-based to USD (F18)
Format details: tcc/document/DD/P1-data.decisions.md (interface lock for the app).

Game keys follow the app's `Game` enum (`ygo` -> `yugioh`). Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
MIRRORED = {"ygo"}  # dec_0013: only Yu-Gi-Oh! images are mirrored (YGOPRODeck forbids hotlinking)
# build.py source key -> app `Game` raw value. Several sources may feed one game (Pokémon EN + JP).
GAME_KEY = {"pokemon": "pokemon", "pokemonJa": "pokemon", "mtg": "mtg", "ygo": "yugioh", "onepiece": "onepiece",
            "lorcana": "lorcana", "dbsFusionWorld": "dbsFusionWorld", "dbsCardGame": "dbsCardGame",
            "digimon": "digimon", "gundam": "gundam", "unionArena": "unionArena", "riftbound": "riftbound",
            "starWarsUnlimited": "starWarsUnlimited"}
HISTORY_BUCKETS = 1024           # one small file per bucket instead of one file per product
HISTORY_DAILY_DAYS = 90          # older points are thinned to the last point of each ISO week
TRENDING_TOP = 50                # movers kept per game
TRENDING_MIN_USD = 1.0           # ignore penny cards: a $0.05 → $0.10 move is not "trending"


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


def package(out: Path, dist: Path, image_base: str | None = None, history_in: Path | None = None) -> dict:
    """`image_base`: public base URL of the CDN. When given, mirrored games point `imageURL` at
    `{image_base}/img/<game>/<file>` and the files are copied to `dist/img/<game>/`. Without it the
    upstream URL is kept (local testing only — never ship YGO that way)."""
    meta = json.loads((out / "meta.json").read_text())
    mirror_path = out / "img" / "manifest.json"
    mirrored = json.loads(mirror_path.read_text()) if mirror_path.exists() else {}
    price_day = meta["prices"]["updated_at"][:10]
    manifest = {"schemaVersion": SCHEMA_VERSION, "generatedAt": meta["generated_at"],
                "priceSource": meta["prices"]["source"], "games": {}}
    by_game: dict[str, list[str]] = collections.defaultdict(list)
    for src_game, game in GAME_KEY.items():
        if ((out / src_game) / "sets.json").exists():
            by_game[game].append(src_game)
    todays: dict[str, dict[str, float]] = {}  # game -> "pid|variant" -> market, for history + trending
    for game, sources in by_game.items():
        cards, prices, seen_prices, set_rows = [], [], set(), []
        for src_game in sources:
          gdir = out / src_game
          set_list = json.loads((gdir / "sets.json").read_text())
          released = {s["id"]: s.get("released") for s in set_list}
          for srow in set_list:
            set_rows.append({"setCode": set_code(src_game, str(srow["id"])), "name": srow["name"],
                             "language": "ja" if src_game == "pokemonJa" else "en",
                             "totalCards": srow.get("card_count", 0), "releaseDate": srow.get("released"),
                             "logoURL": srow.get("logo")})
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
                  elif image_base and src_game in MIRRORED:
                      image = None  # not mirrored yet: never hotlink (YGOPRODeck terms); next nightly run fills it
                  variants = [p["variant"] for p in c.get("prices", [])]
                  cards.append({
                      "id": c["id"].replace(f"{src_game}:", f"{game}:", 1),
                      "name": c["name"], "setCode": set_code(src_game, c["set_id"]), "setName": c["set_name"],
                      "number": c["number"], "rarity": c.get("rarity"),
                      "passcode": str(c["passcode"]) if c.get("passcode") else None,
                      "imageURL": image, "tcgplayerProductId": c.get("tcgplayer_product_id"),
                      "variants": variants, "joinAmbiguous": bool(c.get("join_ambiguous")),
                      "releaseDate": released.get(c["set_id"]),
                      "language": c.get("language", "en"),
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
        todays[game] = {f"{p['productId']}|{p['variant']}": p["market"] for p in prices if p["market"] is not None}
        sets_rel = f"v1/{game}/sets.json"
        (dist / sets_rel).parent.mkdir(parents=True, exist_ok=True)
        set_rows.sort(key=lambda r: (r["releaseDate"] or "", r["setCode"]), reverse=True)
        (dist / sets_rel).write_text(json.dumps({"game": game, "sets": set_rows}, ensure_ascii=False, indent=0))
        stats = {"cards": 0, "with_market_price": 0}
        for src_game in sources:
            for k in stats:
                stats[k] += meta["games"].get(src_game, {}).get(k, 0)
        rate = (stats.get("with_market_price", 0) / stats["cards"]) if stats.get("cards") else None
        manifest["games"][game] = {
            "catalogVersion": version, "catalogURL": cat_rel, "catalogBytes": cat_bytes, "cardCount": len(cards),
            "priceDate": price_day, "pricesURL": price_rel, "pricesBytes": price_bytes,
            "priceMatchRate": round(rate, 4) if rate is not None else None, "setsURL": sets_rel,
        }
    (dist / "v1").mkdir(parents=True, exist_ok=True)
    if history_in is not None:
        manifest.update(write_history(history_in, dist, todays, price_day))
    fx_path = out / "fx.json"
    if fx_path.exists():
        fx = json.loads(fx_path.read_text())
        fx_rel = f"v1/fx-{fx['date']}.json"
        (dist / fx_rel).write_text(json.dumps(fx, indent=1))
        manifest["fxURL"], manifest["fxDate"] = fx_rel, fx["date"]
    (dist / "v1/manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def _week(day: str) -> tuple[int, int]:
    return dt.date.fromisoformat(day).isocalendar()[:2]


def write_history(history_in: Path, dist: Path, todays: dict[str, dict[str, float]], day: str) -> dict:
    """Append today's market prices to the bucketed history, recording a point only when the value
    changed. Returns manifest fields. `history_in` = the previously published v1/history (may not exist)."""
    meta_in = history_in / "meta.json"
    hmeta = json.loads(meta_in.read_text()) if meta_in.exists() else {"firstDay": day, "lastDay": None, "days": []}
    prev_day = hmeta.get("lastDay")
    if prev_day == day:  # same tcgcsv snapshot published twice: nothing new
        prev_day = next((d for d in reversed(hmeta.get("days", [])) if d < day), None)
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=HISTORY_DAILY_DAYS)).isoformat()
    trending: dict[str, list] = {}
    for game, today in todays.items():
        buckets: dict[int, dict] = collections.defaultdict(dict)
        for key, market in today.items():
            buckets[int(key.split("|", 1)[0]) % HISTORY_BUCKETS][key] = market
        src_dir = history_in / game
        movers = []
        bucket_ids = set(buckets) | {int(f.name.split(".")[0]) for f in src_dir.glob("*.json.gz")} if src_dir.exists() else set(buckets)
        for b in bucket_ids:
            f = src_dir / f"{b}.json.gz"
            series = json.loads(gzip.decompress(f.read_bytes()))["series"] if f.exists() else {}
            for key, market in buckets.get(b, {}).items():
                points = series.setdefault(key, [])
                if points and prev_day and points[-1][0] <= prev_day and market >= TRENDING_MIN_USD:
                    old = points[-1][1]  # value as of the previous run (points are written on change only)
                    if old > 0 and old != market:
                        pid, variant = key.split("|", 1)
                        movers.append({"productId": int(pid), "variant": variant, "market": market,
                                       "changePercent": round((market - old) / old * 100, 2)})
                if points and points[-1][0] == day:
                    points[-1][1] = market
                elif not points or points[-1][1] != market:
                    points.append([day, market])
            for key, points in series.items():  # thin old points to one per ISO week
                old_pts = [p for p in points if p[0] < cutoff]
                if len(old_pts) > 1:
                    last_of_week = {}
                    for p in old_pts:
                        last_of_week[_week(p[0])] = p
                    series[key] = sorted(last_of_week.values()) + [p for p in points if p[0] >= cutoff]
            write_gz(dist / f"v1/history/{game}/{b}.json.gz", {"game": game, "bucket": b, "series": series})
        movers.sort(key=lambda m: abs(m["changePercent"]), reverse=True)
        trending[game] = movers[:TRENDING_TOP]
    days = sorted(set(hmeta.get("days", [])) | {day})
    hmeta = {"firstDay": hmeta.get("firstDay") or day, "lastDay": day, "days": days[-400:],
             "buckets": HISTORY_BUCKETS, "dailyDays": HISTORY_DAILY_DAYS}
    (dist / "v1/history").mkdir(parents=True, exist_ok=True)
    (dist / "v1/history/meta.json").write_text(json.dumps(hmeta))
    fields = {"historyURL": "v1/history/meta.json", "historyFirstDay": hmeta["firstDay"],
              "historyBuckets": HISTORY_BUCKETS}
    if prev_day:  # trending needs a previous run to compare with
        rel = f"v1/trending-{day}.json"
        (dist / rel).write_text(json.dumps({"date": day, "comparedTo": prev_day, "games": trending}))
        fields.update({"trendingURL": rel, "trendingDate": day})
    return fields


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--dist", type=Path, default=HERE / "dist")
    ap.add_argument("--image-base", help="public CDN base URL; rewrites mirrored image URLs to it")
    ap.add_argument("--history-in", type=Path,
                    help="previously published v1/history to extend (enables history + trending)")
    a = ap.parse_args()
    m = package(a.out, a.dist, a.image_base, a.history_in)
    for g, e in m["games"].items():
        print(f"{g:17} {e['cardCount']:6} cards  catalog {e['catalogBytes']/1024:7.1f} KB  "
              f"prices {e['pricesBytes']/1024:6.1f} KB  market {e['priceMatchRate']}")


if __name__ == "__main__":
    main()
