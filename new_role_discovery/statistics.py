from __future__ import annotations

import math
from collections.abc import Iterable


def two_proportion_pvalue(x1: int, n1: int, x2: int, n2: int) -> float:
    if n1 <= 0 or n2 <= 0:
        return 1.0
    pooled = (x1 + x2) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if variance <= 0:
        return 1.0
    z_value = (x2 / n2 - x1 / n1) / math.sqrt(variance)
    return min(1.0, math.erfc(abs(z_value) / math.sqrt(2.0)))


def _log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_pvalue(x1: int, n1: int, x2: int, n2: int) -> float:
    """Two-sided Fisher exact p-value for a 2x2 table."""
    if n1 <= 0 or n2 <= 0:
        return 1.0
    successes = x1 + x2
    total = n1 + n2
    minimum = max(0, successes - n2)
    maximum = min(n1, successes)

    def probability(value: int) -> float:
        return math.exp(
            _log_choose(n1, value)
            + _log_choose(n2, successes - value)
            - _log_choose(total, successes)
        )

    observed = probability(x1)
    value = sum(
        probability(candidate)
        for candidate in range(minimum, maximum + 1)
        if probability(candidate) <= observed + 1e-12
    )
    return min(1.0, value)


def compare_proportions(x1: int, n1: int, x2: int, n2: int) -> tuple[str, float]:
    expected = [
        n1 * (x1 + x2) / max(1, n1 + n2),
        n1 * (n1 + n2 - x1 - x2) / max(1, n1 + n2),
        n2 * (x1 + x2) / max(1, n1 + n2),
        n2 * (n1 + n2 - x1 - x2) / max(1, n1 + n2),
    ]
    if min(expected, default=0) < 5:
        return "fisher_exact", fisher_exact_pvalue(x1, n1, x2, n2)
    return "two_proportion_z", two_proportion_pvalue(x1, n1, x2, n2)


def add_bh_qvalues(rows: list[dict], p_key: str = "p_value", q_key: str = "q_value") -> None:
    if not rows:
        return
    ranked = sorted(enumerate(rows), key=lambda item: float(item[1][p_key]))
    total = len(ranked)
    running = 1.0
    for reverse_index in range(total - 1, -1, -1):
        original_index, row = ranked[reverse_index]
        rank = reverse_index + 1
        running = min(running, float(row[p_key]) * total / rank)
        rows[original_index][q_key] = running


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def weighted_jaccard(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    if not keys:
        return 0.0
    numerator = sum(min(left.get(key, 0.0), right.get(key, 0.0)) for key in keys)
    denominator = sum(max(left.get(key, 0.0), right.get(key, 0.0)) for key in keys)
    return numerator / denominator if denominator else 0.0


def jensen_shannon_divergence(
    left_counts: dict[str, int] | Iterable[tuple[str, int]],
    right_counts: dict[str, int] | Iterable[tuple[str, int]],
) -> float:
    left = dict(left_counts)
    right = dict(right_counts)
    keys = set(left) | set(right)
    left_total = sum(left.values())
    right_total = sum(right.values())
    if not keys or left_total <= 0 or right_total <= 0:
        return 0.0
    divergence = 0.0
    for key in keys:
        p = left.get(key, 0) / left_total
        q = right.get(key, 0) / right_total
        midpoint = (p + q) / 2.0
        if p > 0:
            divergence += 0.5 * p * math.log2(p / midpoint)
        if q > 0:
            divergence += 0.5 * q * math.log2(q / midpoint)
    return divergence

