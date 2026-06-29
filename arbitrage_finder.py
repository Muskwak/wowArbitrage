#!/usr/bin/env python3
"""
Cross-server vendor arbitrage finder for World of Warcraft.

Uses Undermine Exchange API (free with Patreon login) to find items
listed on the AH below their vendor sell price across all servers.

Usage:
    1. Get an API key at https://undermine.exchange/ (free, sign in with Patreon)
    2. Run: python arbitrage_finder.py --api-key YOUR_KEY --region us

To expand the vendor price database:
    python arbitrage_finder.py --api-key KEY --fetch-vendor-prices --blizzard-id ID --blizzard-secret SECRET
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import gzip
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Optional

API_BASE = "https://api.undermine.exchange"
VENDOR_PRICE_FILE = os.path.join(os.path.dirname(__file__), "vendor_prices.json")
PRICE_CACHE_LUA = os.path.join(os.path.dirname(__file__), "Arbitrage", "PriceCache.lua")


@dataclass
class Arbitrage:
    item_id: int
    vendor_price: int
    ah_price: int
    quantity: int
    realms: list[str]
    profit_per_item: int
    total_profit: int
    margin_pct: float


def load_vendor_prices() -> dict[int, int]:
    prices = {}
    if os.path.exists(VENDOR_PRICE_FILE):
        with open(VENDOR_PRICE_FILE) as f:
            prices = {int(k): v for k, v in json.load(f).items()}
    if os.path.exists(PRICE_CACHE_LUA):
        with open(PRICE_CACHE_LUA) as f:
            for match in re.finditer(r'\["(\d+)"\]\s*=\s*(\d+)', f.read()):
                item_id, price = int(match.group(1)), int(match.group(2))
                prices.setdefault(item_id, price)
    return prices


def save_vendor_prices(prices: dict[int, int]):
    os.makedirs(os.path.dirname(VENDOR_PRICE_FILE), exist_ok=True)
    with open(VENDOR_PRICE_FILE, "w") as f:
        json.dump(dict(sorted(prices.items())), f, indent=2)
    print(f"  Saved {len(prices)} vendor prices")


def fetch_blizzard_vendor_prices(client_id: str, client_secret: str) -> dict[int, int]:
    """Scrape item IDs with vendor prices from Blizzard's static data."""
    print("Fetching vendor prices from Blizzard API...")
    token_data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request("https://oauth.battle.net/token", data=token_data)
    req.add_header("Authorization", f"Basic {base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req) as resp:
            token = json.load(resp)["access_token"]
    except Exception as e:
        print(f"  Blizzard auth failed: {e}")
        return {}

    headers = {"Authorization": f"Bearer {token}"}
    prices = {}
    found = 0
    batch_size = 100

    for start in range(1, 250000, batch_size):
        ids_param = ",".join(str(i) for i in range(start, start + batch_size))
        url = f"https://us.api.blizzard.com/data/wow/item?ids={ids_param}&namespace=static-us&locale=en_US"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                for item in json.load(resp).get("items", []):
                    sp = item.get("sell_price")
                    if sp and sp > 0:
                        prices[item["id"]] = sp
                        found += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  Rate limited, sleeping...")
                import time
                time.sleep(60)
                continue
            if e.code != 404:
                print(f"  Error at {start}: {e}")

        if start % 10000 == 1:
            print(f"  Scanned items up to {start}, found {found} vendor prices...")

    print(f"  Found {len(prices)} items with vendor prices")
    return prices


def ue_get(path: str, api_key: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"ApiKey {api_key}")
    req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return json.loads(data)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            err = json.loads(body)
            print(f"  API Error: {err}", file=sys.stderr)
        except json.JSONDecodeError:
            print(f"  HTTP {e.code}: {body.decode(errors='replace')[:200]}", file=sys.stderr)
        sys.exit(1)


def copper_str(copper: int) -> str:
    g, r = divmod(copper, 10000)
    s, c = divmod(r, 100)
    if g:
        return f"{g}g {s}s {c}c"
    if s:
        return f"{s}s {c}c"
    return f"{c}c"


def main():
    parser = argparse.ArgumentParser(description="WoW cross-server vendor arbitrage finder")
    parser.add_argument("--api-key", required=True, help="Undermine Exchange API key")
    parser.add_argument("--region", default="us", choices=["us", "eu", "tw", "kr"])
    parser.add_argument("--min-profit", type=int, default=10000, help="Min profit per item in copper (default: 1g)")
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--blizzard-id")
    parser.add_argument("--blizzard-secret")
    parser.add_argument("--fetch-vendor-prices", action="store_true",
                        help="Fetch missing vendor prices from Blizzard API")
    parser.add_argument("--check-items", nargs="+", type=int,
                        help="Check specific item IDs instead of full scan")
    args = parser.parse_args()

    vendor_prices = load_vendor_prices()
    print(f"Loaded {len(vendor_prices)} vendor sell prices")

    if args.fetch_vendor_prices:
        if not args.blizzard_id or not args.blizzard_secret:
            print("Error: --blizzard-id and --blizzard-secret required")
            sys.exit(1)
        new = fetch_blizzard_vendor_prices(args.blizzard_id, args.blizzard_secret)
        vendor_prices.update(new)
        save_vendor_prices(vendor_prices)
        return

    print(f"Fetching region-wide commodity prices for {args.region}...")
    data = ue_get(f"/v1/region/{args.region}/commodities.json", args.api_key)
    commodities = data["result"]["commodities"]
    print(f"  Got {len(commodities)} commodities")

    if args.check_items:
        candidates = {}
        for iid in args.check_items:
            info = commodities.get(str(iid))
            if info:
                vp = vendor_prices.get(iid)
                if vp and vp > 0:
                    candidates[iid] = {"vendor_price": vp, "ah_price": info.get("price", 0), "quantity": info.get("quantity", 0)}
        print(f"  {len(candidates)} target items with vendor prices")
    else:
        candidates = {}
        for id_str, info in commodities.items():
            iid = int(id_str)
            vp = vendor_prices.get(iid)
            if vp and vp > 0:
                ap = info.get("price", 0)
                q = info.get("quantity", 0)
                if ap > 0 and q > 0 and ap < vp:
                    candidates[iid] = {"vendor_price": vp, "ah_price": ap, "quantity": q}
        print(f"  {len(candidates)} items below vendor price region-wide")

    if not candidates:
        print("\nNo opportunities found. Try --fetch-vendor-prices to expand database.")
        return

    print(f"\nFetching per-realm pricing for {len(candidates)} candidates...")
    opportunities = []

    for i, (iid, cinfo) in enumerate(candidates.items()):
        try:
            now = ue_get(f"/v1/region/{args.region}/commodities/{iid}/now.json", args.api_key)
        except Exception:
            continue

        groups = now.get("result", [])
        if not groups:
            continue

        best = groups[0]
        ap = best.get("price", 0)
        qty = best.get("quantity", 0)
        realms = best.get("realms", [])
        vp = cinfo["vendor_price"]

        if ap > 0 and qty > 0 and ap < vp:
            opportunities.append(Arbitrage(
                item_id=iid,
                vendor_price=vp,
                ah_price=ap,
                quantity=qty,
                realms=realms,
                profit_per_item=vp - ap,
                total_profit=(vp - ap) * qty,
                margin_pct=((vp - ap) / ap) * 100,
            ))

        if (i + 1) % 25 == 0:
            print(f"  Checked {i + 1}/{len(candidates)}...")

    opportunities.sort(key=lambda o: o.total_profit, reverse=True)
    opportunities = [o for o in opportunities if o.profit_per_item >= args.min_profit][:args.max_results]

    if not opportunities:
        print("\nNo qualifying opportunities after per-realm check.")
        return

    print(f"\n{'='*120}")
    print(f"VENDOR ARBITRAGE - {args.region.upper()}")
    print(f"{'='*120}")
    header = f"{'Item ID':<10} {'AH Price':<14} {'Vendor':<14} {'Profit/ea':<14} {'Qty':<8} {'Total':<14} {'Margin':<8} {'Realms'}"
    print(header)
    print("-" * 120)

    all_total = 0
    for o in opportunities:
        all_total += o.total_profit
        realms_str = ", ".join(o.realms[:4])
        if len(o.realms) > 4:
            realms_str += f" (+{len(o.realms) - 4} more)"
        print(f"{o.item_id:<10} {copper_str(o.ah_price):<14} {copper_str(o.vendor_price):<14} {copper_str(o.profit_per_item):<14} {o.quantity:<8} {copper_str(o.total_profit):<14} {o.margin_pct:>6.1f}%  {realms_str}")

    print("-" * 120)
    print(f"{'Total profit:':<60} {copper_str(all_total)}")

    out = f"arbitrage_{args.region}.json"
    with open(out, "w") as f:
        json.dump([asdict(o) for o in opportunities], f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
