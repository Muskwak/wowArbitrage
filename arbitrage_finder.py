#!/usr/bin/env python3
"""
Cross-server vendor arbitrage finder for World of Warcraft.

Uses Undermine Exchange API (free with Patreon login) to find items
listed on the AH below their vendor sell price across all servers.

Usage:
    1. Get an API key at https://undermine.exchange/ (free, sign in with Patreon)
    2. python arbitrage_finder.py --api-key KEY --region us

To expand vendor prices:
    python arbitrage_finder.py --api-key KEY --fetch-vendor-prices --blizzard-id ID --blizzard-secret SECRET
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import gzip
from dataclasses import dataclass, asdict
from typing import Optional

API_BASE = "https://api.undermine.exchange"
VENDOR_PRICE_FILE = os.path.join(os.path.dirname(__file__), "vendor_prices.json")
PRICE_CACHE_LUA = os.path.join(os.path.dirname(__file__), "Arbitrage", "PriceCache.lua")


def load_vendor_prices() -> dict[int, int]:
    prices = {}
    if os.path.exists(VENDOR_PRICE_FILE):
        with open(VENDOR_PRICE_FILE) as f:
            prices = {int(k): v for k, v in json.load(f).items()}
    if os.path.exists(PRICE_CACHE_LUA):
        with open(PRICE_CACHE_LUA) as f:
            for match in re.finditer(r'\["(\d+)"\]\s*=\s*(\d+)', f.read()):
                iid, price = int(match.group(1)), int(match.group(2))
                prices.setdefault(iid, price)
    return prices


def save_vendor_prices(prices: dict[int, int]):
    os.makedirs(os.path.dirname(VENDOR_PRICE_FILE), exist_ok=True)
    with open(VENDOR_PRICE_FILE, "w") as f:
        json.dump(dict(sorted(prices.items())), f, indent=2)
    print(f"  Saved {len(prices)} vendor prices to {VENDOR_PRICE_FILE}")


def ue_get(path: str, api_key: str) -> dict:
    req = urllib.request.Request(f"{API_BASE}{path}")
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


def get_blizzard_token(client_id: str, client_secret: str) -> str:
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request("https://oauth.battle.net/token", data=data)
    req.add_header("Authorization", f"Basic {base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["access_token"]


def fetch_vendor_prices_from_ah(api_key: str, region: str,
                                 client_id: str, client_secret: str) -> dict[int, int]:
    """
    Smart vendor price fetch: batch-lookup missing vendor prices for items on the AH.
    """
    token = get_blizzard_token(client_id, client_secret)
    existing = load_vendor_prices()
    print(f"  Already have {len(existing)} vendor prices")

    print(f"  Fetching active AH items from Undermine Exchange...")
    comms = ue_get(f"/v1/region/{region}/commodities.json", api_key)
    ah_ids = sorted(int(k) for k in comms["result"]["commodities"].keys())

    print(f"  {len(ah_ids)} commodities on AH")
    missing = [iid for iid in ah_ids if iid not in existing]
    print(f"  {len(missing)} items need vendor price lookup")

    if not missing:
        print("  All items already have vendor prices!")
        return existing

    found = 0
    batch_size = 100
    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        ids_param = ",".join(str(iid) for iid in batch)
        url = f"https://us.api.blizzard.com/data/wow/item?ids={ids_param}&namespace=static-us&locale=en_US"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req) as resp:
                for item in json.load(resp).get("items", []):
                    sp = item.get("sell_price")
                    if sp and sp > 0:
                        existing[item["id"]] = sp
                        found += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  Rate limited, waiting 30s...")
                time.sleep(30)
                continue
            if e.code != 404:
                print(f"  Error at batch {i}: {e}")

        if (i + batch_size) % 500 == 0 or i + batch_size >= len(missing):
            print(f"  {min(i + batch_size, len(missing))}/{len(missing)} checked, {found} new prices")

    print(f"  Found {found} new vendor prices ({len(existing)} total)")
    save_vendor_prices(existing)
    return existing


def copper_str(copper: int) -> str:
    g, r = divmod(copper, 10000)
    s, c = divmod(r, 100)
    if g: return f"{g}g {s}s {c}c"
    if s: return f"{s}s {c}c"
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
                        help="Fetch missing vendor prices from Blizzard API (only for items on AH)")
    parser.add_argument("--check-items", nargs="+", type=int,
                        help="Check specific item IDs instead of full scan")
    args = parser.parse_args()

    # Load vendor prices
    vendor_prices = load_vendor_prices()
    print(f"Loaded {len(vendor_prices)} vendor sell prices")

    if args.fetch_vendor_prices:
        if not args.blizzard_id or not args.blizzard_secret:
            print("Error: --blizzard-id and --blizzard-secret required")
            sys.exit(1)
        fetch_vendor_prices_from_ah(args.api_key, args.region,
                                     args.blizzard_id, args.blizzard_secret)
        return

    # Fetch region-wide commodity prices
    print(f"Fetching commodity prices for {args.region}...")
    data = ue_get(f"/v1/region/{args.region}/commodities.json", args.api_key)
    commodities = data["result"]["commodities"]
    print(f"  {len(commodities)} commodities tracked")

    # Find candidates where region-min price < vendor price
    candidates = {}
    for id_str, info in commodities.items():
        iid = int(id_str)
        vp = vendor_prices.get(iid)
        if not vp or vp <= 0:
            continue
        ap = info.get("price", 0)
        q = info.get("quantity", 0)
        if ap > 0 and q > 0 and ap < vp:
            candidates[iid] = {"vendor_price": vp, "ah_price": ap, "quantity": q}

    if args.check_items:
        candidates = {iid: candidates[iid] for iid in args.check_items if iid in candidates}
    print(f"  {len(candidates)} items below vendor price region-wide")

    if not candidates:
        print("\nNo opportunities found. Try --fetch-vendor-prices to expand database.")
        return

    # Get per-item detail for each candidate
    print(f"Fetching item-level pricing...")
    results = []
    for i, (iid, cinfo) in enumerate(candidates.items()):
        try:
            now = ue_get(f"/v1/region/{args.region}/commodities/{iid}/now.json", args.api_key)
        except Exception:
            continue
        info = now.get("result", {})
        if isinstance(info, list):
            info = info[0] if info else {}
        ap = info.get("price", 0)
        qty = info.get("quantity", 0)
        vp = cinfo["vendor_price"]
        if ap > 0 and qty > 0 and ap < vp:
            results.append({
                "item_id": iid,
                "vendor_price": vp,
                "ah_price": ap,
                "quantity": qty,
                "profit_per_item": vp - ap,
                "total_profit": (vp - ap) * qty,
                "margin_pct": round(((vp - ap) / ap) * 100, 1),
            })
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(candidates)} checked ({len(results)} opportunities found)")

    results.sort(key=lambda o: o["total_profit"], reverse=True)
    results = [o for o in results if o["profit_per_item"] >= args.min_profit][:args.max_results]

    if not results:
        print("\nNo qualifying opportunities.")
        return

    print(f"\n{'='*100}")
    print(f"VENDOR ARBITRAGE - {args.region.upper()} (region-wide min prices)")
    print(f"{'='*100}")
    print(f"{'ID':<8} {'AH Price':<14} {'Vendor':<14} {'Profit/ea':<14} {'Qty':<8} {'Total':<14} {'Margin':<8}")
    print("-" * 100)

    grand_total = 0
    for o in results:
        grand_total += o["total_profit"]
        print(f"{o['item_id']:<8} {copper_str(o['ah_price']):<14} {copper_str(o['vendor_price']):<14} "
              f"{copper_str(o['profit_per_item']):<14} {o['quantity']:<8} "
              f"{copper_str(o['total_profit']):<14} {o['margin_pct']:>6.1f}%")

    print("-" * 100)
    print(f"{'Potential profit (region):':<70} {copper_str(grand_total)}")
    print(f"{'Items found:':<70} {len(results)}")
    print(f"\n  Prices shown are region-wide minimums. Deal exists on at least one realm.")
    print(f"  Use --realm to check a specific realm, or check item IDs on undermine.exchange")

    out = f"arbitrage_{args.region}.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
