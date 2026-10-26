#!/usr/bin/env python3
"""
WHAT:  Sample sizes and detectable differences for ML / NLP experiments: how
       big a test set, how many seeds, how many judged pairs -- and, once
       results exist, a paired bootstrap and Holm's correction.
WHY:   "Is 1 point on 500 test items a real difference?" has an answer, and
       it is usually "no". A planner should compute it, not guess it.
CONCEPT: Normal approximations, standard library only. Planning numbers, not
       a substitute for the test you will actually run.

Usage:
    ml_power.py accuracy --n 500 --acc 0.80                 # smallest gap a paired test detects
    ml_power.py accuracy --gap 0.02 --acc 0.80 --disagree 0.10   # test items needed
    ml_power.py seeds --sd 0.8 --gap 1.0                    # runs per system (scores in points)
    ml_power.py seeds --sd 0.8 --runs 5                     # smallest gap with 5 runs each
    ml_power.py winrate --wins 130 --n 200                  # win rate with a 95% interval
    ml_power.py winrate --halfwidth 0.05                    # judged pairs for +-5 points
    ml_power.py bootstrap a.txt b.txt                       # paired: per-item scores, one per line
    ml_power.py holm 0.01 0.04 0.03                         # several comparisons at once
Common options: --alpha 0.05 --power 0.8 (two-sided).
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from statistics import NormalDist, mean, stdev

Z = NormalDist().inv_cdf


def _z(alpha: float, power: float) -> float:
    return Z(1 - alpha / 2) + Z(power)


def accuracy(n: int | None, acc: float, gap: float | None, disagree: float | None,
             alpha: float, power: float) -> dict:
    """Two systems scored on the SAME test items (paired; McNemar). What
    matters is how often they disagree: q = P(exactly one is right). Without
    a measured q, errors are assumed independent -- q = 2·acc·(1-acc) -- which
    overstates it for similar systems (they fail on the same items), so the
    numbers are conservative."""
    q = disagree if disagree is not None else 2 * acc * (1 - acc)
    k = _z(alpha, power)
    out = {"disagreement_rate": round(q, 4),
           "disagreement_from": "given" if disagree is not None else "assumed independent errors"}
    if gap is not None:
        out["test_items_needed"] = math.ceil(k * k * q / (gap * gap))
    if n is not None:
        out["detectable_gap"] = round(k * math.sqrt(q / n), 4)
        out["detectable_gap_points"] = round(100 * k * math.sqrt(q / n), 2)
    return out


def seeds(sd: float, gap: float | None, runs: int | None, alpha: float, power: float) -> dict:
    """Two systems, each trained `runs` times with different seeds; compare the
    mean scores (Welch/t). `sd` is the spread of one system's score across
    seeds -- measure it from a pilot of 3-5 runs, or take it from papers that
    report it."""
    k = _z(alpha, power)
    out = {}
    if gap is not None:
        out["runs_per_system"] = max(2, math.ceil(2 * (k * sd / gap) ** 2) + 1)
    if runs is not None:
        out["detectable_gap"] = round(k * sd * math.sqrt(2 / max(runs - 1, 1)), 3)
    return out


def winrate(wins: int | None, n: int | None, halfwidth: float | None, p: float,
            alpha: float) -> dict:
    """Pairwise preference (human or LLM judge): A beats B in `wins` of `n`.
    Wilson interval; ties counted half, or dropped and said so."""
    z = Z(1 - alpha / 2)
    out = {}
    if wins is not None and n:
        ph = wins / n
        centre = (ph + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        out.update(win_rate=round(ph, 4), interval=[round(centre - half, 4), round(centre + half, 4)],
                   beats_coin_flip=centre - half > 0.5 or centre + half < 0.5)
    if halfwidth is not None:
        out["pairs_needed"] = math.ceil(z * z * p * (1 - p) / (halfwidth * halfwidth))
    return out


def bootstrap(a: list[float], b: list[float], samples: int, alpha: float, seed: int) -> dict:
    """Paired bootstrap over test items: per-item scores of systems A and B on
    the same items (1/0 for accuracy, or any per-item metric)."""
    if len(a) != len(b) or not a:
        raise SystemExit("bootstrap needs the same number of per-item scores for A and B")
    d = [x - y for x, y in zip(a, b)]
    rng = random.Random(seed)
    n = len(d)
    means = sorted(mean(rng.choice(d) for _ in range(n)) for _ in range(samples))
    lo, hi = means[int(alpha / 2 * samples)], means[int((1 - alpha / 2) * samples) - 1]
    # Two-sided p: how often a resampled difference falls on the far side of 0.
    obs = mean(d)
    flipped = sum(1 for m in means if (m <= 0 if obs > 0 else m >= 0))
    return {"items": n, "mean_difference": round(obs, 5), "interval": [round(lo, 5), round(hi, 5)],
            "p_value": round(min(1.0, 2 * flipped / samples), 4) if flipped else f"< {1 / samples:g}",
            "sd_of_item_differences": round(stdev(d), 5) if n > 1 else None}


def holm(pvalues: list[float], alpha: float) -> list[dict]:
    """Holm-Bonferroni: several datasets, metrics or comparisons at once."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    m = len(pvalues)
    out = [None] * m
    running, stop = 0.0, False
    for rank, i in enumerate(order):
        adjusted = min(1.0, max(running, (m - rank) * pvalues[i]))
        running = adjusted
        stop = stop or adjusted > alpha
        out[i] = {"p": pvalues[i], "adjusted": round(adjusted, 5), "significant": not stop}
    return out


def _scores(path: str) -> list[float]:
    with open(path, encoding="utf-8") as f:
        return [float(line) for line in f if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--alpha", type=float, default=0.05)
    common.add_argument("--power", type=float, default=0.8)
    p = sub.add_parser("accuracy", parents=[common])
    p.add_argument("--n", type=int)
    p.add_argument("--acc", type=float, required=True)
    p.add_argument("--gap", type=float, help="as a fraction: 0.02 = 2 points")
    p.add_argument("--disagree", type=float, help="P(exactly one system right), if measured")
    p = sub.add_parser("seeds", parents=[common])
    p.add_argument("--sd", type=float, required=True)
    p.add_argument("--gap", type=float)
    p.add_argument("--runs", type=int)
    p = sub.add_parser("winrate", parents=[common])
    p.add_argument("--wins", type=int)
    p.add_argument("--n", type=int)
    p.add_argument("--halfwidth", type=float)
    p.add_argument("--p", type=float, default=0.5, help="expected win rate, for --halfwidth")
    p = sub.add_parser("bootstrap", parents=[common])
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--samples", type=int, default=10000)
    p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("holm", parents=[common])
    p.add_argument("p", type=float, nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "accuracy":
        if args.n is None and args.gap is None:
            parser.error("accuracy needs --n (to get the detectable gap) or --gap (to get n)")
        out = accuracy(args.n, args.acc, args.gap, args.disagree, args.alpha, args.power)
    elif args.cmd == "seeds":
        if args.gap is None and args.runs is None:
            parser.error("seeds needs --gap (to get runs) or --runs (to get the detectable gap)")
        out = seeds(args.sd, args.gap, args.runs, args.alpha, args.power)
    elif args.cmd == "winrate":
        out = winrate(args.wins, args.n, args.halfwidth, args.p, args.alpha)
    elif args.cmd == "bootstrap":
        out = bootstrap(_scores(args.a), _scores(args.b), args.samples, args.alpha, args.seed)
    else:
        out = holm(args.p, args.alpha)
    rows = out if isinstance(out, list) else [out]
    for row in rows:
        print("  ".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
