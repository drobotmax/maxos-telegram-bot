#!/usr/bin/env python3
"""Aggregate router-log.jsonl into a usage profile.

Reads data/router-log.jsonl, groups by kind/model, prints:
- request count, total cost, avg cost, avg turns, avg time
- distribution by msg_len buckets (proxy for complexity)

Use to identify which slice of traffic is heavy enough to keep on Opus
vs. which slice is mechanical and can move to Haiku.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

LOG = Path(__file__).resolve().parent.parent / "data" / "router-log.jsonl"


def bucket(msg_len: int) -> str:
    if msg_len < 100:
        return "xs (<100)"
    if msg_len < 500:
        return "s (100-500)"
    if msg_len < 2000:
        return "m (500-2k)"
    if msg_len < 10000:
        return "l (2k-10k)"
    return "xl (10k+)"


def main(days: int = 7) -> None:
    if not LOG.exists():
        print(f"No log yet at {LOG}")
        return

    cutoff = datetime.now().astimezone() - timedelta(days=days)
    rows = []
    with LOG.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
                if ts < cutoff:
                    continue
            except ValueError:
                pass
            rows.append(r)

    if not rows:
        print(f"No records in last {days}d")
        return

    print(f"=== Router stats: last {days}d, {len(rows)} requests ===\n")

    # By kind
    print("By kind:")
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r.get("kind", "?")].append(r)
    for kind, items in by_kind.items():
        cost = sum(i.get("cost_usd", 0) for i in items)
        errs = sum(1 for i in items if i.get("error"))
        avg_turns = sum(i.get("turns", 0) for i in items) / max(len(items), 1)
        avg_time = sum(i.get("time_s", 0) for i in items) / max(len(items), 1)
        print(f"  {kind}: n={len(items)} cost=${cost:.2f} avg_turns={avg_turns:.1f} avg_time={avg_time:.1f}s errs={errs}")

    # By msg_len bucket
    print("\nBy msg_len bucket:")
    by_b = defaultdict(list)
    for r in rows:
        by_b[bucket(r.get("msg_len", 0))].append(r)
    for b in ["xs (<100)", "s (100-500)", "m (500-2k)", "l (2k-10k)", "xl (10k+)"]:
        items = by_b.get(b, [])
        if not items:
            continue
        cost = sum(i.get("cost_usd", 0) for i in items)
        avg_cost = cost / len(items)
        avg_turns = sum(i.get("turns", 0) for i in items) / len(items)
        print(f"  {b}: n={len(items)} total=${cost:.2f} avg=${avg_cost:.4f} avg_turns={avg_turns:.1f}")

    # Top expensive requests
    print("\nTop 10 most expensive requests:")
    top = sorted(rows, key=lambda r: r.get("cost_usd", 0), reverse=True)[:10]
    for r in top:
        print(f"  ${r.get('cost_usd', 0):.4f} turns={r.get('turns', 0)} msg_len={r.get('msg_len', 0)} chat={r.get('chat_id')} kind={r.get('kind')}")

    total_cost = sum(r.get("cost_usd", 0) for r in rows)
    print(f"\nTotal spend last {days}d: ${total_cost:.2f}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    main(days)
