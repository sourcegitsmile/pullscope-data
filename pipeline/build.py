#!/usr/bin/env python3
"""Card-data buffer for TCG Collector — spike.

Pulls card metadata from each game's community source, joins it to TCGplayer prices
mirrored by tcgcsv.com, and writes static JSON the app (or a CDN) can serve.

    python3 build.py                     # default sample sets, all 5 games
    python3 build.py --game ygo --sets "Legend of Blue Eyes White Dragon"
    python3 build.py --all-sets          # every set (CI only — thousands of requests)

Stdlib only, no keys, no secrets. Terms per source: see spike/REPORT.md §(a)(b).
Output layout (out/):
    meta.json                      generated_at, price freshness, sources, join stats
    <game>/sets.json               sets built in this run
    <game>/<set_id>.json           cards with prices
    <game>/index.json              compact lookup rows for on-device OCR matching
    images.json                    card id -> upstream image URL (mirror_images.py downloads)
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "TCGCollector-DataBuffer/0.1 (spike)"  # Scryfall requires an accurate User-Agent
HOST_DELAY = {  # seconds between requests, per each source's stated guidance
    "api.scryfall.com": 0.1,       # Scryfall: 50–100 ms
    "api.lorcast.com": 0.1,        # Lorcast: 50–100 ms
    "db.ygoprodeck.com": 0.06,     # YGOPRODeck: ≤ 20 req/s
    "tcgcsv.com": 0.25,            # tcgcsv sample script pauses 0.25 s
    "api.tcgdex.net": 0.2,
    "optcgapi.com": 0.2,
    "www.ecb.europa.eu": 0.5,
}
TCGCSV = "https://tcgcsv.com/tcgplayer"
CATEGORY = {"mtg": 1, "ygo": 2, "pokemon": 3, "onepiece": 68, "lorcana": 71,
            # F20 (PRD 2.0): games with no other free card source — catalog AND prices from tcgcsv.
            "dbsFusionWorld": 80, "dbsCardGame": 27, "digimon": 63, "gundam": 86, "unionArena": 81,
            "riftbound": 89, "starWarsUnlimited": 79,
            "pokemonJa": 85}  # Pokémon Japan → packaged into `pokemon` with language "ja"
TCGCSV_ONLY = ["dbsFusionWorld", "dbsCardGame", "digimon", "gundam", "unionArena", "riftbound",
               "starWarsUnlimited", "pokemonJa"]
LANGUAGE = {"pokemonJa": "ja"}
DEFAULT_GROUPS = 2  # local runs without --all-sets: newest N groups of each tcgcsv-only game
ECB_FX = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_CODES = ["USD", "JPY", "GBP", "EUR", "CAD", "AUD", "NZD", "SGD"]  # F18
DEFAULT_SETS = {
    "pokemon": ["base1", "swsh3", "sv08"],
    "mtg": ["lea", "fdn"],
    "ygo": ["Legend of Blue Eyes White Dragon", "Rarity Collection 5"],
    "onepiece": ["OP-01", "OP-05"],
    "lorcana": ["1", "11"],
}

# ---------------------------------------------------------------- http

_last_hit: dict[str, float] = {}


def get_json(url: str, cache_dir: Path, retries: int = 5, ttl_h: float = 20):
    """GET with per-host pacing, retry/backoff on 429/5xx, and an on-disk cache."""
    key = hashlib.sha1(url.encode()).hexdigest()
    cached = cache_dir / f"{key}.json.gz"
    if cached.exists() and time.time() - cached.stat().st_mtime < ttl_h * 3600:
        return json.loads(gzip.decompress(cached.read_bytes()))
    host = urllib.parse.urlparse(url).hostname or ""
    for attempt in range(retries):
        wait = HOST_DELAY.get(host, 0.2) - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            data = json.loads(body)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(gzip.compress(body))
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(min(30, 1.5 * 2 ** attempt))
    return None


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode().strip()

# ---------------------------------------------------------------- normalisation


def norm(s: str | None) -> str:
    """Lowercase ASCII alphanumerics only; 'Pokémon' -> 'pokemon'."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def num_key(s: str | None) -> str:
    """'004/191' -> '4/191', '0806' -> '806', 'OP01-001' -> 'op01001'."""
    s = (s or "").strip().lower()
    if "/" in s:
        a, b = s.split("/", 1)
        return f"{a.lstrip('0') or '0'}/{b.lstrip('0') or '0'}"
    if re.fullmatch(r"\d+[a-z]?", s):
        return s.lstrip("0") or "0"
    return norm(s)


def strip_group_prefix(name: str) -> str:
    return re.sub(r"^[A-Z0-9]{2,6}:\s*", "", name)  # "SV08: Surging Sparks" -> "Surging Sparks"

# ---------------------------------------------------------------- tcgcsv (prices)


class TcgCsv:
    def __init__(self, cache: Path):
        self.cache = cache

    def groups(self, game: str) -> list[dict]:
        return get_json(f"{TCGCSV}/{CATEGORY[game]}/groups", self.cache)["results"]

    def find_group(self, game: str, *, name: str | None = None, abbr: str | None = None,
                   group_id: int | None = None) -> dict | None:
        gs = self.groups(game)
        if group_id:
            return next((g for g in gs if g["groupId"] == group_id), None)
        if abbr:
            hit = [g for g in gs if norm(g.get("abbreviation")) == norm(abbr)]
            if hit:
                return hit[0]
        if name:
            key = re.sub(r"^the", "", norm(name))
            exact = [g for g in gs if re.sub(r"^the", "", norm(strip_group_prefix(g["name"]))) == key]
            return exact[0] if exact else None
        return None

    def find_groups(self, game: str, *, name: str, abbr_prefix: str | None) -> list[dict]:
        """All groups for one printed set — TCGplayer splits YGO reprints (LOB / LOB-E / LOB-EN)."""
        key = re.sub(r"^the", "", norm(name))
        out = [g for g in self.groups(game)
               if re.sub(r"^the", "", norm(strip_group_prefix(g["name"]))).startswith(key)
               or (abbr_prefix and norm(g.get("abbreviation")).startswith(norm(abbr_prefix)))]
        return out

    def products(self, game: str, group_id: int) -> list[dict]:
        return get_json(f"{TCGCSV}/{CATEGORY[game]}/{group_id}/products", self.cache)["results"]

    def prices(self, game: str, group_id: int) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for p in get_json(f"{TCGCSV}/{CATEGORY[game]}/{group_id}/prices", self.cache)["results"]:
            out.setdefault(p["productId"], []).append({
                "variant": p["subTypeName"], "market": p["marketPrice"],
                "low": p["lowPrice"], "mid": p["midPrice"], "high": p["highPrice"]})
        return out


def ext(product: dict, field: str) -> str | None:
    return next((e["value"] for e in product.get("extendedData", []) if e["name"] == field), None)

# ---------------------------------------------------------------- adapters
# Each adapter returns (set_info, cards, group_id). A card dict carries an optional
# "tcgplayer_product_id"; the join step attaches prices from that product.


def card(game, set_id, set_name, src_id, name, number, rarity, image, source, product_id=None, **extra):
    return {"id": f"{game}:{src_id}", "game": game, "set_id": set_id, "set_name": set_name,
            "name": name, "number": number, "rarity": rarity, "image": image,
            "data_source": source, "tcgplayer_product_id": product_id, **extra}


def build_pokemon(set_id, csv: TcgCsv, cache):
    s = get_json(f"https://api.tcgdex.net/v2/en/sets/{set_id}", cache)
    if not s:
        raise RuntimeError(f"TCGdex set {set_id} not found / unavailable")
    group = csv.find_group("pokemon", name=s["name"])
    # TCGdex set listing lacks TCGplayer ids → join on (name, card number) against the group's products.
    by_key: dict[tuple, int] = {}
    if group:
        for p in csv.products("pokemon", group["groupId"]):
            n = ext(p, "Number")
            if n:
                base_name = re.sub(r"\s*-\s*\d+/\d+.*$", "", p["name"])  # "Pikachu - 057/191" -> "Pikachu"
                base_name = re.sub(r"\s*\(.*?\)\s*$", "", base_name)
                by_key.setdefault((norm(base_name), n.split("/")[0].lstrip("0") or "0"), p["productId"])
    official = s["cardCount"].get("official")
    cards = []
    for c in s["cards"]:
        local = c["localId"]
        number = f"{local}/{official}" if official and local.isdigit() else local
        pid = by_key.get((norm(c["name"]), local.lstrip("0") or "0"))
        cards.append(card("pokemon", set_id, s["name"], c["id"], c["name"], number, None,
                          f"{c['image']}/high.webp" if c.get("image") else None, "TCGdex", pid))
    logo = f"{s['logo']}.png" if s.get("logo") else None
    return {"id": set_id, "name": s["name"], "released": s.get("releaseDate"), "logo": logo}, cards, group


def build_mtg(set_code, csv: TcgCsv, cache):
    s = get_json(f"https://api.scryfall.com/sets/{set_code}", cache)
    group = csv.find_group("mtg", group_id=s.get("tcgplayer_id")) or csv.find_group("mtg", name=s["name"])
    cards, url = [], s["search_uri"]
    while url:
        page = get_json(url, cache)
        for c in page["data"]:
            img = (c.get("image_uris") or (c.get("card_faces") or [{}])[0].get("image_uris") or {}).get("normal")
            cards.append(card("mtg", set_code, s["name"], c["id"], c["name"], c["collector_number"], c["rarity"],
                              img, "Scryfall", c.get("tcgplayer_id"),
                              scryfall_prices={k: v for k, v in c["prices"].items() if v}))
        url = page.get("next_page") if page.get("has_more") else None
    return {"id": set_code, "name": s["name"], "released": s.get("released_at")}, cards, group


def build_ygo(set_name, csv: TcgCsv, cache):
    q = urllib.parse.quote(set_name)
    data = (get_json(f"https://db.ygoprodeck.com/api/v7/cardinfo.php?cardset={q}", cache) or {}).get("data", [])
    prefix = next((cs["set_code"].split("-")[0] for c in data for cs in c.get("card_sets", [])
                   if cs["set_name"] == set_name), None)
    groups = csv.find_groups("ygo", name=set_name, abbr_prefix=prefix)
    group = groups[0] if groups else None
    by_code: dict[tuple, int] = {}
    for g in groups:
        for p in csv.products("ygo", g["groupId"]):
            n, r = ext(p, "Number"), ext(p, "Rarity")
            if n:
                by_code.setdefault((norm(n), norm(r)), p["productId"])
                by_code.setdefault((norm(n), ""), p["productId"])
    cards, set_id = [], None
    for c in data:
        for cs in c.get("card_sets", []):
            if cs["set_name"] != set_name:
                continue
            set_id = set_id or cs["set_code"].split("-")[0]
            pid = by_code.get((norm(cs["set_code"]), norm(cs["set_rarity"]))) or by_code.get((norm(cs["set_code"]), ""))
            img = c["card_images"][0]["image_url"] if c.get("card_images") else None
            cards.append(card("ygo", set_id, set_name, f"{cs['set_code']}-{norm(cs['set_rarity'])}", c["name"],
                              cs["set_code"], cs["set_rarity"], img, "YGOPRODeck", pid, passcode=c["id"]))
    return {"id": set_id or norm(set_name), "name": set_name, "released": None,
            "extra_groups": [g["groupId"] for g in groups[1:]]}, cards, group


PARALLEL = re.compile(r"parallel|alternate art", re.I)


def build_onepiece(set_id, csv: TcgCsv, cache):
    data = get_json(f"https://optcgapi.com/api/sets/{set_id}/", cache) or []
    group = csv.find_group("onepiece", abbr=set_id.replace("-", ""))
    by_key: dict[tuple, list[int]] = {}
    if group:
        for p in csv.products("onepiece", group["groupId"]):
            n = ext(p, "Number")
            if n:
                parallel = bool(PARALLEL.search(p["name"]))
                by_key.setdefault((norm(n), parallel), []).append(p["productId"])
    cards, seen = [], {}
    for c in data:
        parallel = bool(PARALLEL.search(c["card_name"]))
        key = (norm(c["card_set_id"]), parallel)
        idx = seen.get(key, 0)
        seen[key] = idx + 1
        cands = by_key.get(key, [])
        pid = cands[idx] if idx < len(cands) else None  # n-th parallel ↔ n-th product: ambiguous, flagged below
        name = re.sub(r"\s*\(\d+\)", "", c["card_name"])
        # Set prefix: SP reprints reuse an older code (OP-05 carries OP01-121). "p" keeps the parallel's id
        # apart from the regular printing's (both start at idx 0).
        src_id = f"{set_id.replace('-', '')}-{c['card_set_id']}-{'p' if parallel else ''}{idx}"
        cards.append(card("onepiece", set_id, c["set_name"], src_id, name,
                          c["card_set_id"], c["rarity"], c["card_image"], "OPTCG API", pid,
                          join_ambiguous=len(cands) > 1))
    name = data[0]["set_name"] if data else set_id
    return {"id": set_id, "name": name, "released": None}, cards, group


def build_lorcana(set_code, csv: TcgCsv, cache):
    sets = get_json("https://api.lorcast.com/v0/sets", cache)["results"]
    s = next(x for x in sets if x["code"] == set_code)
    data = get_json(f"https://api.lorcast.com/v0/sets/{set_code}/cards", cache)
    group = csv.find_group("lorcana", name=s["name"])
    cards = []
    for c in data:
        title = f"{c['name']} - {c['version']}" if c.get("version") else c["name"]
        img = ((c.get("image_uris") or {}).get("digital") or {}).get("normal")
        cards.append(card("lorcana", set_code, s["name"], c["id"], title, c["collector_number"], c.get("rarity"),
                          img, "Lorcast", c.get("tcgplayer_id")))
    return {"id": set_code, "name": s["name"], "released": s.get("released_at")}, cards, group


def tcgcsv_adapter(game: str):
    """Card list straight from tcgcsv groups/products (F20 games, Pokémon JP). A product is a CARD when it
    carries a printed "Number"; sealed product (boxes, packs, decks) has none and is skipped."""
    def build(group_id, csv: TcgCsv, cache):
        group = next((g for g in csv.groups(game) if str(g["groupId"]) == str(group_id)), None)
        if not group:
            raise RuntimeError(f"tcgcsv group {group_id} not found in category {CATEGORY[game]}")
        set_id = (group.get("abbreviation") or str(group["groupId"])).strip()
        if game in LANGUAGE:
            set_id = f"{LANGUAGE[game]}-{set_id}"
        cards = []
        for p in csv.products(game, group["groupId"]):
            number = ext(p, "Number")
            if not number:
                continue
            rarity = ext(p, "Rarity")
            image = (p.get("imageUrl") or "").replace("_200w.", "_400w.") or None
            src_id = f"{LANGUAGE[game]}-{p['productId']}" if game in LANGUAGE else str(p["productId"])
            cards.append(card(game, set_id, group["name"], src_id, p["name"], number,
                              None if rarity in (None, "", "None") else rarity, image, "tcgcsv",
                              p["productId"], language=LANGUAGE.get(game, "en")))
        released = (group.get("publishedOn") or "")[:10] or None
        return {"id": set_id, "name": group["name"], "released": released, "logo": None}, cards, group
    return build


ADAPTERS = {"pokemon": build_pokemon, "mtg": build_mtg, "ygo": build_ygo,
            "onepiece": build_onepiece, "lorcana": build_lorcana,
            **{g: tcgcsv_adapter(g) for g in TCGCSV_ONLY}}


def fetch_fx(cache: Path) -> dict | None:
    """ECB euro reference rates, re-based to USD (F18). ECB allows reuse with the source named."""
    try:
        xml = get_text(ECB_FX)
    except Exception as e:  # FX is optional: the app falls back to USD
        print(f"  ! fx: {e}", file=sys.stderr)
        return None
    day = re.search(r"time='(\d{4}-\d{2}-\d{2})'", xml)
    eur = {m.group(1): float(m.group(2)) for m in re.finditer(r"currency='([A-Z]{3})' rate='([0-9.]+)'", xml)}
    eur["EUR"] = 1.0
    if not day or "USD" not in eur:
        return None
    usd = eur["USD"]
    return {"date": day.group(1), "base": "USD", "source": "European Central Bank euro foreign exchange reference rates",
            "rates": {c: round(eur[c] / usd, 6) for c in FX_CODES if c in eur}}


def all_set_ids(game: str, cache: Path) -> list[str]:
    if game == "pokemon":
        return [s["id"] for s in get_json("https://api.tcgdex.net/v2/en/sets", cache)]
    if game == "mtg":
        return [s["code"] for s in get_json("https://api.scryfall.com/sets", cache)["data"] if not s.get("digital")]
    if game == "ygo":
        return sorted({s["set_name"] for s in get_json("https://db.ygoprodeck.com/api/v7/cardsets.php", cache)})
    if game in TCGCSV_ONLY:
        return [str(g["groupId"]) for g in get_json(f"{TCGCSV}/{CATEGORY[game]}/groups", cache)["results"]]
    if game == "onepiece":
        return [s["set_id"] for s in get_json("https://optcgapi.com/api/allSets/", cache)]
    return [s["code"] for s in get_json("https://api.lorcast.com/v0/sets", cache)["results"]]

# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", choices=list(ADAPTERS), action="append")
    ap.add_argument("--sets", nargs="*", help="set ids for the (single) --game")
    ap.add_argument("--all-sets", action="store_true")
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--cache", type=Path, default=HERE / ".cache")
    a = ap.parse_args()

    csv = TcgCsv(a.cache)
    games = a.game or list(ADAPTERS)
    price_updated = get_text("https://tcgcsv.com/last-updated.txt")
    meta = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prices": {"source": "TCGplayer market data via tcgcsv.com", "currency": "USD",
                       "updated_at": price_updated},
            "games": {}}
    images: dict[str, str] = {}
    a.out.mkdir(parents=True, exist_ok=True)

    for game in games:
        if a.sets and len(games) == 1:
            set_ids = a.sets
        elif a.all_sets:
            set_ids = all_set_ids(game, a.cache)
        elif game in TCGCSV_ONLY:  # newest groups only, for local smoke runs
            groups = sorted(csv.groups(game), key=lambda g: g.get("publishedOn") or "", reverse=True)
            set_ids = [str(g["groupId"]) for g in groups[:DEFAULT_GROUPS]]
        else:
            set_ids = DEFAULT_SETS[game]
        gdir = a.out / game
        gdir.mkdir(exist_ok=True)
        stats = {"sets": 0, "cards": 0, "with_product": 0, "with_market_price": 0, "ambiguous": 0, "failed_sets": []}
        set_rows, index = [], []
        for sid in set_ids:
            try:
                info, cards, group = ADAPTERS[game](sid, csv, a.cache)
            except Exception as e:  # one bad set must not sink the nightly run
                stats["failed_sets"].append(f"{sid}: {type(e).__name__}: {e}"[:200])
                print(f"  ! {game} {sid}: {e}", file=sys.stderr)
                continue
            prices = csv.prices(game, group["groupId"]) if group else {}
            for gid in info.get("extra_groups", []):
                prices.update(csv.prices(game, gid))
            for c in cards:
                c["prices"] = prices.get(c["tcgplayer_product_id"], []) if c["tcgplayer_product_id"] else []
                stats["with_product"] += c["tcgplayer_product_id"] is not None
                stats["with_market_price"] += any(p["market"] is not None for p in c["prices"])
                stats["ambiguous"] += bool(c.get("join_ambiguous"))
                if c["image"]:
                    images[c["id"]] = c["image"]
                index.append([c["id"], norm(c["name"]), num_key(c["number"]), info["id"]])
            info["tcgcsv_group"] = group["groupId"] if group else None
            info["card_count"] = len(cards)
            set_rows.append(info)
            stats["sets"] += 1
            stats["cards"] += len(cards)
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(info["id"]))
            (gdir / f"{safe}.json").write_text(json.dumps({"set": info, "cards": cards}, ensure_ascii=False))
            print(f"  {game:9} {str(info['id']):8} {len(cards):5} cards  group={info['tcgcsv_group']}")
        (gdir / "sets.json").write_text(json.dumps(set_rows, ensure_ascii=False, indent=1))
        (gdir / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
        meta["games"][game] = stats

    fx = fetch_fx(a.cache)
    if fx:
        (a.out / "fx.json").write_text(json.dumps(fx, indent=1))
    (a.out / "images.json").write_text(json.dumps(images, separators=(",", ":")))
    (a.out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta["games"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
