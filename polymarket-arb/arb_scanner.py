"""
Polymarket Arbitrage Scanner

Scans active binary (YES/NO) markets on Polymarket and detects when the
combined ask prices of YES + NO are less than $1.00, which represents a
risk-free arbitrage opportunity at resolution (one side always pays $1).

Educational tool only. Real execution requires:
  - Sufficient depth on both sides (use --min-size)
  - Account for gas + trading fees
  - Capital lockup until market resolves
  - Settlement/oracle risk

API endpoints:
  - Gamma API (markets metadata): https://gamma-api.polymarket.com/markets
  - CLOB API (order books):       https://clob.polymarket.com/book

Usage:
    python arb_scanner.py
    python arb_scanner.py --min-edge 0.005 --min-size 100 --limit 500
    python arb_scanner.py --loop 30   # rescan every 30 seconds
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"

HEADERS = {"User-Agent": "polymarket-arb-scanner/1.0"}


@dataclass
class Opportunity:
    question: str
    slug: str
    yes_ask: float
    no_ask: float
    yes_size: float
    no_size: float
    total: float
    edge: float
    max_shares: float
    profit_per_dollar: float
    url: str


def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Page through Gamma API and return active, non-closed binary markets."""
    markets: list[dict] = []
    offset = 0
    page = 100
    while len(markets) < limit:
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": page,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            r = requests.get(GAMMA_API, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ! gamma fetch failed at offset {offset}: {e}", file=sys.stderr)
            break

        batch = r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < page:
            break
        offset += page

    return markets[:limit]


def get_token_ids(market: dict) -> Optional[tuple[str, str]]:
    """Extract YES and NO CLOB token IDs from a market record."""
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(ids, list) or len(ids) != 2:
        return None
    return ids[0], ids[1]


def fetch_book(token_id: str) -> Optional[dict]:
    try:
        r = requests.get(CLOB_BOOK, params={"token_id": token_id}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def best_ask(book: dict) -> Optional[tuple[float, float]]:
    """Return (price, size) for the lowest ask, or None if empty."""
    asks = book.get("asks") or []
    if not asks:
        return None
    # Polymarket returns asks sorted; lowest is typically last item.
    # Be safe and pick the minimum price.
    best = min(asks, key=lambda lvl: float(lvl["price"]))
    return float(best["price"]), float(best["size"])


def scan(
    limit: int,
    min_edge: float,
    min_size: float,
    verbose: bool,
) -> list[Opportunity]:
    print(f"  - fetching up to {limit} active markets…")
    markets = fetch_active_markets(limit=limit)
    print(f"  - scanning {len(markets)} markets for YES+NO < $1")

    opportunities: list[Opportunity] = []

    for i, m in enumerate(markets, 1):
        if verbose and i % 25 == 0:
            print(f"    [{i}/{len(markets)}] checked, {len(opportunities)} hits")

        if m.get("closed") or not m.get("active"):
            continue

        token_pair = get_token_ids(m)
        if not token_pair:
            continue
        yes_tok, no_tok = token_pair

        yes_book = fetch_book(yes_tok)
        no_book = fetch_book(no_tok)
        if not yes_book or not no_book:
            continue

        yes_best = best_ask(yes_book)
        no_best = best_ask(no_book)
        if not yes_best or not no_best:
            continue

        yes_ask, yes_size = yes_best
        no_ask, no_size = no_best

        # Sanity: prices must be in (0,1)
        if not (0 < yes_ask < 1 and 0 < no_ask < 1):
            continue

        total = yes_ask + no_ask
        edge = 1.0 - total
        if edge < min_edge:
            continue

        max_shares = min(yes_size, no_size)
        if max_shares < min_size:
            continue

        slug = m.get("slug", "")
        opp = Opportunity(
            question=m.get("question", "(unknown)"),
            slug=slug,
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_size=yes_size,
            no_size=no_size,
            total=total,
            edge=edge,
            max_shares=max_shares,
            profit_per_dollar=edge / total if total > 0 else 0.0,
            url=f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com",
        )
        opportunities.append(opp)

    opportunities.sort(key=lambda o: o.profit_per_dollar, reverse=True)
    return opportunities


def print_opportunities(opps: list[Opportunity]) -> None:
    if not opps:
        print("\nNo arbitrage opportunities found.\n")
        return

    print()
    print("=" * 96)
    print(f"  FOUND {len(opps)} ARBITRAGE OPPORTUNITIES")
    print("=" * 96)

    for i, o in enumerate(opps, 1):
        # Naive capital estimate: cost to buy `max_shares` of each side
        capital = o.max_shares * o.total
        guaranteed_payout = o.max_shares  # exactly $1 per share-pair at resolution
        guaranteed_profit = guaranteed_payout - capital

        print(f"\n[{i}] {o.question}")
        print(f"     YES ask: ${o.yes_ask:.4f}  (size {o.yes_size:.1f})")
        print(f"     NO  ask: ${o.no_ask:.4f}  (size {o.no_size:.1f})")
        print(f"     Total : ${o.total:.4f}   →  edge ${o.edge:.4f}   ({o.profit_per_dollar * 100:.2f}% on capital)")
        print(f"     Max risk-free fill: {o.max_shares:.2f} share-pairs")
        print(f"        capital required: ${capital:.2f}")
        print(f"        guaranteed profit at resolution: ${guaranteed_profit:.2f}")
        print(f"     {o.url}")

    print("\n" + "=" * 96)
    print("  NOTE: profit estimates ignore trading fees, gas, and capital lockup.")
    print("=" * 96 + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket arbitrage scanner")
    p.add_argument("--limit", type=int, default=300, help="Max markets to scan (default 300)")
    p.add_argument("--min-edge", type=float, default=0.005,
                   help="Minimum (1 - YES_ask - NO_ask) edge to report (default 0.005 = 0.5%%)")
    p.add_argument("--min-size", type=float, default=10.0,
                   help="Minimum share size available on both sides (default 10)")
    p.add_argument("--loop", type=int, default=0,
                   help="Rescan every N seconds (0 = single run)")
    p.add_argument("--verbose", action="store_true", help="Print progress while scanning")
    args = p.parse_args()

    while True:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] scanning Polymarket…")
        try:
            opps = scan(args.limit, args.min_edge, args.min_size, args.verbose)
            print_opportunities(opps)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0
        except Exception as e:
            print(f"  ! scan failed: {e}", file=sys.stderr)

        if args.loop <= 0:
            return 0
        print(f"  - sleeping {args.loop}s before next scan… (Ctrl+C to stop)")
        try:
            time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
