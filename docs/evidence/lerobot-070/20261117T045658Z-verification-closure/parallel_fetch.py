"""Chunked parallel download of one large Hub file (fineart model.safetensors).

The mirror throttles a single connection to ~1-2 MiB/s and sometimes stalls for
minutes; a 9.35 GB file then takes hours. This splits the file into fixed-size
ranges, downloads them concurrently into the right offsets of a preallocated
file, and verifies the whole-file SHA-256 against the LFS digest from the Hub
API, so speed comes from parallelism and correctness is still proven by hash.

Usage:
    python parallel_fetch.py URL DEST SHA256 SIZE [--threads 16] [--chunk-mb 32]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

done_lock = threading.Lock()
done_bytes = 0


def _fetch_range(url: str, start: int, end: int, fd: int, attempt: int = 5) -> int:
    for i in range(attempt):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
                data = resp.read()
            if len(data) != end - start + 1:
                raise IOError(f"short read {len(data)} != {end - start + 1}")
            os.pwrite(fd, data, start)
            return len(data)
        except Exception as exc:  # noqa: BLE001 - retried with backoff
            if i == attempt - 1:
                raise
            print(f"  retry {i + 1} for {start}-{end}: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3 + 3 * i)
    return 0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("dest")
    ap.add_argument("expected_sha256")
    ap.add_argument("size", type=int)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--chunk-mb", type=int, default=32)
    args = ap.parse_args()

    if os.path.exists(args.dest) and os.path.getsize(args.dest) == args.size:
        print(f"size already {args.size}; verifying hash")
        return 0 if _sha256(args.dest) == args.expected_sha256 else 2

    chunk = args.chunk_mb << 20
    ranges = [(s, min(s + chunk - 1, args.size - 1)) for s in range(0, args.size, chunk)]
    fd = os.open(args.dest, os.O_RDWR | os.O_CREAT, 0o644)
    os.ftruncate(fd, args.size)
    started = time.time()
    total = 0
    try:
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {pool.submit(_fetch_range, args.url, s, e, fd): (s, e) for s, e in ranges}
            for i, fut in enumerate(as_completed(futures), 1):
                n = fut.result()
                total += n
                if i % 20 == 0 or i == len(ranges):
                    elapsed = max(time.time() - started, 1e-6)
                    print(f"{i}/{len(ranges)} chunks  {total / 2**30:.2f} GiB  "
                          f"{total / 2**20 / elapsed:.1f} MiB/s", flush=True)
    finally:
        os.close(fd)

    digest = _sha256(args.dest)
    print(f"sha256 {digest}")
    if digest != args.expected_sha256:
        print(f"MISMATCH expected {args.expected_sha256}", file=sys.stderr)
        return 3
    print("hash OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
