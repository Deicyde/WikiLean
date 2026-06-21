#!/usr/bin/env python3
"""Alignment-safe full differential test: real CPython Z13701 vs the compiled Lean
embedding, diffed PER CHUNK (fresh process each, with retries) so a slow/timed-out
chunk can never misalign the comparison. Same guarantee as difftest.py, robust to
the embedding interpreter's per-process slowdown on many large-number cases."""
import os, subprocess, sys

def Z13701(a, b):
    while b != 0:
        a, b = b, a % b
    return a == 1

LEAN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "lean"))
BINARY = os.path.join(LEAN_DIR, ".lake", "build", "bin", "diffdriver")
CHUNK, TIMEOUT, RETRIES = 25, 120, 5


def cases():
    cs = [(a, b) for a in range(0, 8) for b in range(0, 8)]
    cs += [(1, n) for n in range(40)] + [(n, 1) for n in range(40)]
    cs += [(n, 0) for n in range(40)] + [(0, n) for n in range(40)]
    import random
    random.seed(0)
    cs += [(random.randint(0, 10**6), random.randint(0, 10**6)) for _ in range(600)]
    cs += [(2**40, 2**40 - 1), (10**12, 7), (0, 10**9), (10**9, 1), (123456, 789012)]
    return cs


def lean_chunk(chunk):
    inp = "".join(f"{a} {b}\n" for a, b in chunk)
    for _ in range(RETRIES):
        try:
            out = subprocess.run([BINARY], cwd=LEAN_DIR, input=inp,
                                 capture_output=True, text=True, timeout=TIMEOUT)
            res = [t for t in out.stdout.split() if t in ("true", "false", "none")]
            if len(res) == len(chunk):
                return res
        except subprocess.TimeoutExpired:
            continue
    return None


def main():
    cs = cases()
    total = mism = unrecoverable = 0
    for i in range(0, len(cs), CHUNK):
        chunk = cs[i:i + CHUNK]
        leans = lean_chunk(chunk)
        if leans is None:
            unrecoverable += len(chunk)
            sys.stderr.write(f"chunk @ {i} unrecoverable after {RETRIES} retries\n")
            continue
        for (a, b), lr in zip(chunk, leans):
            total += 1
            if ("true" if Z13701(a, b) else "false") != lr:
                mism += 1
                print(f"  MISMATCH a={a} b={b} lean={lr} python={'true' if Z13701(a,b) else 'false'}")
    print(f"{total} compared, {mism} mismatches, {unrecoverable} unrecoverable "
          f"(real CPython vs compiled Lean embedding)")
    sys.exit(0 if mism == 0 and unrecoverable == 0 else 2)


if __name__ == "__main__":
    main()
