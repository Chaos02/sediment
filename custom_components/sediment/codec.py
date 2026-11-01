"""Sediment codec: decides which `states` rows of one entity survive a tier.

Pure Python, standard library only, no Home Assistant imports. Ported from the measurement
prototype this integration grew out of, with its fixes:

* a numeric series with at most three distinct values is treated as "few level" (enum
  rule, every transition kept) only when the level separation is clearly above the
  sensor quantum; otherwise it goes through the continuous rule (resolution flicker);
* the invariant is: exact extremes of the tier input in active buckets, 2E for samples in
  flat buckets (compounding across tiers makes 2E exact only for the first tier);
* the relink rule lives in `recorder_bridge` (HA's own NULL `old_state_id` stay NULL);
* a gap is a run of `unavailable`/`unknown` rows longer than five minutes; its edges stay.

A series is given as parallel lists `t` (float epoch seconds, ascending), `v` (float or
None for a non numeric or gap row) and `s` (state string). Every function returns sorted
positions into the lists it was given.
"""

from __future__ import annotations

from bisect import bisect_right
from itertools import pairwise
import math
from typing import NamedTuple

GAP_STATES = frozenset(("unavailable", "unknown", "", "None"))
GAP_S = 300.0  # gaps longer than 5 minutes keep their edges
# A bucket is active only if its range exceeds E by more than float noise: 20.1 - 20.0 is
# 0.10000000000000142, which must not count as more than one quantum of 0.1.
QUANTUM_REL_TOL = 1.0 + 1e-9

# A numeric series with at most this many distinct values is a few-level candidate.
FEWLEVEL_MAX_LEVELS = 3
# ... and stays few-level (enum rule) only if the closest two levels are further apart
# than this many sensor quanta. Below that it is resolution flicker (0.00 / 0.01).
FEWLEVEL_MIN_SEPARATION_Q = 5.0

ATTR_DRIVEN: dict[str, tuple[str, ...]] = {
    "climate": ("current_temperature", "temperature", "hvac_action"),
}
ENUM_DOMAINS = frozenset(
    (
        "binary_sensor",
        "switch",
        "light",
        "input_boolean",
        "automation",
        "script",
        "person",
        "device_tracker",
        "zone",
        "media_player",
        "update",
        "select",
        "button",
        "image",
        "notify",
        "sun",
        "group",
        "input_select",
        "lock",
        "cover",
    )
)


class TierCfg(NamedTuple):
    """One thinning tier: bucket seconds, tolerance factors, longest SDT segment."""

    bucket: float
    m: float
    f: float
    compmax: float


DEFAULT_TIERS = (
    TierCfg(900.0, 1.0, 0.01, 6 * 3600.0),
    TierCfg(3600.0, 2.0, 0.02, 86400.0),
    TierCfg(21600.0, 4.0, 0.05, 7 * 86400.0),
)


def tier_for_bucket(bucket: float) -> TierCfg:
    """Tolerance factors for a bucket size: the nearest reference tier at or below it."""
    chosen = DEFAULT_TIERS[0]
    for cfg in DEFAULT_TIERS:
        if bucket >= cfg.bucket:
            chosen = cfg
    return TierCfg(float(bucket), chosen.m, chosen.f, chosen.compmax)


# ---------------------------------------------------------------- classification
def to_float(s: object) -> float | None:
    """Parse a state string to a finite float, else None."""
    try:
        x = float(s)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def decimals(s: str) -> int:
    """Number of decimals written in a plain numeric state string."""
    if "e" in s or "E" in s:
        return 6
    i = s.find(".")
    return 0 if i < 0 else len(s) - i - 1


def classify(domain: str, states: list[str], state_class: str | None) -> str:
    """Return enum, counter, fewlevel, continuous or attribute for one entity."""
    if domain in ATTR_DRIVEN:
        return "attribute"
    live = [x for x in states if x not in GAP_STATES]
    nn = len(live)
    if nn == 0:
        return "enum"
    vals = [to_float(x) for x in live]
    isnum = sum(1 for x in vals if x is not None)
    if domain in ENUM_DOMAINS or isnum < 0.9 * nn:
        return "enum"
    if state_class in ("total_increasing", "total"):
        return "counter"
    distinct = {x for x in vals if x is not None}
    if len(distinct) <= FEWLEVEL_MAX_LEVELS:
        return "fewlevel" if levels_far_apart(live, distinct) else "continuous"
    return "continuous"


def levels_far_apart(live: list[str], distinct: set[float]) -> bool:
    """True if the closest two levels are many quanta apart (a real few-level signal)."""
    if len(distinct) < 2:
        return True
    dec = max(decimals(x) for x in set(live))
    quantum = 10.0 ** (-dec)
    lv = sorted(distinct)
    sep = min(b - a for a, b in pairwise(lv))
    return sep > FEWLEVEL_MIN_SEPARATION_Q * quantum


def numeric_view(states: list[str]) -> list[float | None]:
    return [None if s in GAP_STATES else to_float(s) for s in states]


# ---------------------------------------------------------------- parameters
def series_params(vals: list[float | None]) -> dict[str, float]:
    """q (quantum), sigma (noise), R (robust range) from the valid numeric values."""
    x = [a for a in vals if a is not None]
    n = len(x)
    if n < 2:
        return {"q": 0.0, "sigma": 0.0, "R": 0.0}
    q = 0.0
    prev = x[0]
    for a in x[1:]:
        d = round(abs(a - prev), 9)
        if d > 0 and (q == 0.0 or d < q):
            q = d
        prev = a
    sigma = 0.0
    if n >= 5:
        res = []
        for i in range(2, n - 2):
            w = x[i - 2 : i + 3]
            if w[0] == w[1] == w[2] == w[3] == w[4]:
                res.append(0.0)
                continue
            w.sort()
            res.append(abs(x[i] - w[2]))
        res.sort()
        sigma = 1.4826 * res[len(res) // 2]
    xs = sorted(x)
    p1 = xs[int(0.01 * (n - 1))]
    p99 = xs[math.ceil(0.99 * (n - 1))]
    return {"q": q, "sigma": sigma, "R": p99 - p1}


def tolerance(p: dict[str, float], m: float, f: float) -> float:
    return max(p["q"], min(m * p["sigma"], f * p["R"]))


# ---------------------------------------------------------------- gaps
def split_segments(
    t: list[float], v: list[float | None], tend: float
) -> tuple[list[list[int]], set[int]]:
    """Return (segments, mandatory).

    segments: lists of positions of valid rows with no long gap inside.
    mandatory: positions that must be kept (gap edges).
    """
    n = len(t)
    segs: list[list[int]] = []
    cur: list[int] = []
    mand: set[int] = set()
    run = -1  # first gap row of the current gap run
    last_valid = -1
    for i in range(n):
        if v[i] is None:
            if run < 0:
                run = i
            continue
        if run >= 0:
            if t[i] - t[run] > GAP_S:
                mand.add(run)
                mand.add(i)
                if last_valid >= 0:
                    mand.add(last_valid)
                if cur:
                    segs.append(cur)
                cur = []
            run = -1
        cur.append(i)
        last_valid = i
    if run >= 0 and tend - t[run] > GAP_S:
        mand.add(run)
        if last_valid >= 0:
            mand.add(last_valid)
    if cur:
        segs.append(cur)
    return segs, mand


# ---------------------------------------------------------------- continuous
def thin_continuous(
    t: list[float],
    v: list[float | None],
    p: dict[str, float],
    cfg: TierCfg,
    tend: float,
) -> tuple[list[int], float, set[int]]:
    """Bucket extremes plus persistent steps, then SDT over the candidate points."""
    W, m, f, compmax = cfg
    E = tolerance(p, m, f)
    sigma = p["sigma"]
    n = len(t)
    if n == 0:
        return [], E, set()
    segs, keep = split_segments(t, v, tend)
    keep = set(keep)
    keep.add(0)
    keep.add(n - 1)
    jthr = max(3 * E, 5 * sigma)
    active: set[int] = set()
    for seg in segs:
        m_ = len(seg)
        pts: list[int] = []
        mand: set[int] = set()
        a = 0
        while a < m_:
            key = int(t[seg[a]] // W)
            b = a
            lo = hi = v[seg[a]]
            ilo = ihi = a
            while b + 1 < m_ and int(t[seg[b + 1]] // W) == key:
                b += 1
                x = v[seg[b]]
                if x < lo:
                    lo, ilo = x, b
                if x > hi:
                    hi, ihi = x, b
            if hi - lo > E * QUANTUM_REL_TOL:
                active.add(key)
                mand.add(seg[ilo])
                mand.add(seg[ihi])
                cand = {a, b, ilo, ihi}
            else:
                cand = {a, b}
            for c in sorted(cand):
                pts.append(seg[c])
            a = b + 1
        if jthr > 0:
            for k in range(m_ - 1):
                i, j = seg[k], seg[k + 1]
                dv = v[j] - v[i]
                if abs(dv) > jthr and t[j] - t[i] <= W / 4:
                    persist = True
                    tj = t[j]
                    half = abs(dv) / 2
                    kk = k + 2
                    while kk < m_:
                        r = seg[kk]
                        if t[r] - tj > W / 4:
                            break
                        if abs(v[r] - v[j]) > half:
                            persist = False
                            break
                        kk += 1
                    if persist:
                        mand.add(i)
                        mand.add(j)
                        pts.append(i)
                        pts.append(j)
        pts = sorted(set(pts))
        keep.update(_sdt(pts, t, v, E, compmax, mand))
    return sorted(keep), E, active


def _sdt(
    pts: list[int],
    t: list[float],
    v: list[float | None],
    E: float,
    compmax: float,
    mand: set[int],
) -> list[int]:
    """Swinging door trending over the candidate points; mandatory points are kept."""
    if not pts:
        return []
    kept = [pts[0]]
    anchor = pts[0]
    ta, va = t[anchor], v[anchor]
    lo_s, hi_s = -math.inf, math.inf
    prev = anchor
    for p in pts[1:]:
        dt = t[p] - ta
        viol = False
        if dt <= 0:
            viol = True
        else:
            s = (v[p] - va) / dt
            if s < lo_s or s > hi_s or dt > compmax:
                viol = True
        if viol and prev != anchor:
            kept.append(prev)
            anchor = prev
            ta, va = t[anchor], v[anchor]
            lo_s, hi_s = -math.inf, math.inf
            dt = t[p] - ta
        if dt > 0:
            lo = (v[p] - E - va) / dt
            hi = (v[p] + E - va) / dt
            lo_s = max(lo_s, lo)
            hi_s = min(hi_s, hi)
        if p in mand:
            kept.append(p)
            anchor = p
            ta, va = t[p], v[p]
            lo_s, hi_s = -math.inf, math.inf
        prev = p
    kept.append(pts[-1])
    return kept


# ---------------------------------------------------------------- counter
def thin_counter(t: list[float], v: list[float | None], cfg: TierCfg, tend: float) -> list[int]:
    """First and last value per bucket, resets, and gap edges; flat buckets dropped."""
    W = cfg[0]
    n = len(t)
    if n == 0:
        return []
    segs, keep = split_segments(t, v, tend)
    keep = set(keep)
    keep.add(0)
    keep.add(n - 1)
    last_kept_v = None
    for seg in segs:
        m_ = len(seg)
        a = 0
        prev_v = None
        while a < m_:
            key = int(t[seg[a]] // W)
            b = a
            while b + 1 < m_ and int(t[seg[b + 1]] // W) == key:
                b += 1
            for k in range(a, b + 1):
                x = v[seg[k]]
                if prev_v is not None and x < prev_v:
                    keep.add(seg[k])
                    keep.add(seg[k - 1] if k > 0 else seg[k])
                    if k == a and a > 0:
                        keep.add(seg[a - 1])
                prev_v = x
            lv = v[seg[b]]
            flat = last_kept_v is not None and all(
                v[seg[k]] == last_kept_v for k in range(a, b + 1)
            )
            if not flat:
                keep.add(seg[a])
                keep.add(seg[b])
            last_kept_v = lv
            a = b + 1
    return sorted(keep)


# ---------------------------------------------------------------- enum
def thin_enum(
    t: list[float],
    s: list[str],
    flap: bool = False,
    flap_gap: float = 60.0,
    flap_min: int = 4,
) -> list[int]:
    """Keep every transition (state differs from the previous row). Optional flap collapse.

    A burst is at least `flap_min` transitions each closer than `flap_gap` seconds to the
    previous one; with `flap` on only the first and last transition of a burst stay.
    """
    n = len(t)
    if n == 0:
        return []
    trans = [0]
    prev = s[0]
    for i in range(1, n):
        if s[i] != prev:
            trans.append(i)
            prev = s[i]
    out = set(trans)
    out.add(n - 1)
    if flap and len(trans) >= flap_min:
        i = 0
        L = len(trans)
        drop: list[int] = []
        while i < L - 1:
            j = i
            while j + 1 < L and t[trans[j + 1]] - t[trans[j]] < flap_gap:
                j += 1
            if j - i + 1 >= flap_min:
                drop.extend(trans[i + 1 : j])
            i = j + 1 if j > i else i + 1
        for d in drop:
            out.discard(d)
    return sorted(out)


# ---------------------------------------------------------------- dispatcher
def thin_tier(
    cls: str,
    t: list[float],
    v: list[float | None] | None,
    s: list[str] | None,
    params: dict[str, float],
    cfg: TierCfg,
    tend: float,
    flap: bool = False,
) -> tuple[list[int], float, set[int]]:
    """One tier on one series. Returns (positions, E, active bucket keys)."""
    if cls == "continuous":
        return thin_continuous(t, v, params, cfg, tend)
    if cls == "counter":
        return thin_counter(t, v, cfg, tend), 0.0, set()
    return thin_enum(t, s, flap=flap), 0.0, set()


def run_chain(
    cls: str,
    t: list[float],
    v: list[float | None] | None,
    s: list[str] | None,
    params: dict[str, float],
    tiers: list[TierCfg] | tuple[TierCfg, ...],
    tend: float,
    flap_from: int | None = None,
) -> list[tuple[list[int], float, set[int]]]:
    """Apply tiers in sequence, each on the output of the previous one.

    Returns one (kept positions in the original index space, E, active keys) per tier.
    `flap_from` is the first tier index at which flapping collapse applies (None = off).
    """
    cur = list(range(len(t)))
    out = []
    for k, cfg in enumerate(tiers):
        tt = [t[i] for i in cur]
        vv = [v[i] for i in cur] if v is not None else None
        ss = [s[i] for i in cur] if s is not None else None
        flap = flap_from is not None and k >= flap_from
        pos, E, act = thin_tier(cls, tt, vv, ss, params, cfg, tend, flap=flap)
        cur = [cur[i] for i in pos]
        out.append((cur, E, act))
    return out


def thin_attribute_chain(
    t: list[float],
    state: list[str],
    comps: list[tuple[str, str, list[float | None] | None, list[str]]],
    params: list[dict[str, float] | None],
    tiers: list[TierCfg] | tuple[TierCfg, ...],
    tend: float,
    flap_from: int | None = None,
) -> list[list[int]]:
    """Attribute driven entities (climate): a row stays if any component keeps it.

    comps: (name, class, numeric values, strings) per component, the entity state first.
    Returns the kept positions (original index space) after every tier.
    """
    n = len(t)
    cur = list(range(n))
    result = []
    for k, cfg in enumerate(tiers):
        tt = [t[i] for i in cur]
        union: set[int] = set()
        for (_name, ccls, num, st), p in zip(comps, params, strict=True):
            ss = [st[i] for i in cur]
            if ccls == "continuous":
                vv = [num[i] for i in cur]
                pos, _e, _a = thin_continuous(tt, vv, p, cfg, tend)
            else:
                pos = thin_enum(tt, ss, flap=flap_from is not None and k >= flap_from)
            union.update(pos)
        if cur:
            union.add(0)
            union.add(len(cur) - 1)
        cur = [cur[i] for i in sorted(union)]
        result.append(cur)
    del state
    return result


# ---------------------------------------------------------------- evaluation helpers
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
