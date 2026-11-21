"""Evaluation helpers for the codec: reconstruction error and counter exactness.

Used by the tests and the measurement tools, not by the job.
"""

from __future__ import annotations

from bisect import bisect_right
import math

from .codec import QUANTUM_REL_TOL


def interp_errors(
    t: list[float],
    v: list[float | None],
    kept: list[int],
    E: float,
    W: float,
    active: set[int],
) -> dict:
    """Linear reconstruction error of every valid raw sample against the kept rows.

    Reports the largest err/E over flat-bucket samples and over all samples, the counts
    above 2E, and the exactness of the bucket extremes for buckets whose range exceeds E.
    """
    kt = [t[i] for i in kept if v[i] is not None]
    kv = [v[i] for i in kept if v[i] is not None]
    nk = len(kt)
    mx_flat = mx_all = 0.0
    n = over2 = over2_flat = nflat = 0
    cur_key = None
    rmin = rmax = imin = imax = None
    ext = {"buckets": 0, "exact": 0, "max_dev": 0.0}
    j = 0

    def close_bucket() -> None:
        if cur_key is None:
            return
        if rmax - rmin > E * QUANTUM_REL_TOL:
            ext["buckets"] += 1
            d = max(rmax - imax, imin - rmin)
            if d <= 1e-12:
                ext["exact"] += 1
            if E > 0:
                ext["max_dev"] = max(ext["max_dev"], d / E)

    for i in range(len(t)):
        x = v[i]
        if x is None:
            continue
        ti = t[i]
        while j + 1 < nk and kt[j + 1] <= ti:
            j += 1
        if nk == 0:
            r = x
        elif ti <= kt[0]:
            r = kv[0]
        elif j + 1 >= nk:
            r = kv[-1]
        else:
            t0, t1 = kt[j], kt[j + 1]
            r = kv[j] if t1 == t0 else kv[j] + (kv[j + 1] - kv[j]) * (ti - t0) / (t1 - t0)
        err = abs(x - r)
        key = int(ti // W)
        flat = key not in active
        ratio = err / E if E > 0 else (0.0 if err == 0 else math.inf)
        n += 1
        mx_all = max(mx_all, ratio)
        if ratio > 2 + 1e-9:
            over2 += 1
        if flat:
            nflat += 1
            mx_flat = max(mx_flat, ratio)
            if ratio > 2 + 1e-9:
                over2_flat += 1
        if key != cur_key:
            close_bucket()
            cur_key = key
            rmin = rmax = x
            imin = imax = r
        else:
            rmin = min(rmin, x)
            rmax = max(rmax, x)
            imin = min(imin, r)
            imax = max(imax, r)
    close_bucket()
    return {
        "n": n,
        "nflat": nflat,
        "max_all": mx_all,
        "max_flat": mx_flat,
        "over2": over2,
        "over2_flat": over2_flat,
        "ext": ext,
    }


def counter_exact(
    t: list[float], v: list[float | None], kept: list[int], W: float
) -> tuple[int, int]:
    """Per bucket: last raw value vs last kept value with key <= bucket (step reconstruction)."""
    last_raw: dict[int, float] = {}
    order: list[int] = []
    for i in range(len(t)):
        if v[i] is None:
            continue
        k = int(t[i] // W)
        if k not in last_raw:
            order.append(k)
        last_raw[k] = v[i]
    kl: dict[int, float] = {}
    for i in kept:
        if v[i] is None:
            continue
        kl[int(t[i] // W)] = v[i]
    ks = sorted(kl)
    bad = tot = 0
    for k in order:
        idx = bisect_right(ks, k) - 1
        rec = kl[ks[idx]] if idx >= 0 else None
        tot += 1
        if rec != last_raw[k]:
            bad += 1
    return tot, bad
