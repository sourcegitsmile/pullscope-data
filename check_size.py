#!/usr/bin/env python3
"""Fail the build before GitHub Pages does: print the site size per top folder, exit 1 over the limit."""
import argparse
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("dist", type=Path)
ap.add_argument("--limit-mb", type=float, default=900)
a = ap.parse_args()
sizes: dict[str, int] = {}
for p in a.dist.rglob("*"):
    if p.is_file():
        top = "/".join(p.relative_to(a.dist).parts[:2])
        sizes[top] = sizes.get(top, 0) + p.stat().st_size
total = sum(sizes.values())
for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:10]:
    print(f"{v / 1e6:9.1f} MB  {k}")
print(f"{total / 1e6:9.1f} MB  total (limit {a.limit_mb:.0f} MB)")
if total > a.limit_mb * 1e6:
    print("::error::Pages site over the size budget. Lower QUALITY/WIDTH in mirror_images.py or split img/ into a second repo (README §Over the limit).")
    sys.exit(1)
