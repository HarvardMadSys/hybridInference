"""Knapsack optimization algorithms for outsourcing decisions.

Ported from vidur's outsourcing module with support for multiple strategies.
"""

import math
import random
from typing import Callable, Dict, List, Set, Tuple


class KnapsackSolver:
    """Solve knapsack problems for outsourcing optimization."""

    def __init__(self, strategy: str = "dp_scaled"):
        """
        Initialize the knapsack solver.

        Args:
            strategy: Selection strategy ("fractional", "dp", "dp_scaled", "random")
        """
        self._strategy = strategy
        self._solver_func = self._get_solver_function(strategy)

    def _get_solver_function(self, strategy: str) -> Callable:
        """Get the appropriate solver function based on strategy."""
        strategies = {
            "fractional": self._solve_fractional,
            "dp": self._solve_dp,
            "dp_scaled": self._solve_dp_scaled,
            "random": self._solve_random,
        }
        if strategy not in strategies:
            raise ValueError(
                f"Unknown knapsack strategy: {strategy}. "
                f"Choose from: {list(strategies.keys())}"
            )
        return strategies[strategy]

    def solve(
        self, items: List[Dict], budget: int
    ) -> Tuple[Set[str], List[str]]:
        """
        Solve the knapsack problem.

        Args:
            items: List of items with "id", "weight", and "value" keys
            budget: Maximum weight capacity

        Returns:
            Tuple of (keep_set, outsource_ids)
        """
        return self._solver_func(items, budget)

    def _solve_fractional(
        self, items: List[Dict], budget: int
    ) -> Tuple[Set[str], List[str]]:
        """
        Greedy fractional knapsack: keep highest value/weight locally.
        """
        items = sorted(
            items, key=lambda x: x["value"] / x["weight"], reverse=True
        )
        keep, total = [], 0
        for it in items:
            if total + it["weight"] <= budget:
                keep.append(it["id"])
                total += it["weight"]
        keep_set = set(keep)
        outsource = [it["id"] for it in items if it["id"] not in keep_set]
        return keep_set, outsource

    def _solve_dp(
        self, items: List[Dict], budget: int
    ) -> Tuple[Set[str], List[str]]:
        """
        0/1 knapsack using dynamic programming.
        """
        if budget <= 0 or not items:
            return set(), [it["id"] for it in items]

        n = len(items)
        dp = [0] * (budget + 1)
        choice = [[False] * (budget + 1) for _ in range(n)]

        # Fill DP table
        for i, it in enumerate(items):
            w = int(it["weight"])
            v = int(it["value"])
            if w <= 0:
                w = 1
            if w > budget:
                continue
            # Iterate backward to avoid reusing item
            for b in range(budget, w, -1):
                if dp[b - w] + v > dp[b]:
                    dp[b] = dp[b - w] + v
                    choice[i][b] = True
            if dp[w] < v:
                dp[w] = v
                choice[i][w] = True

        # Reconstruct solution
        b = max(range(budget + 1), key=lambda x: dp[x])
        keep_ids = []
        for i in range(n - 1, -1, -1):
            if choice[i][b]:
                keep_ids.append(items[i]["id"])
                b -= int(items[i]["weight"]) if items[i]["weight"] > 0 else 1

        keep_set = set(keep_ids)
        outsource_ids = [it["id"] for it in items if it["id"] not in keep_set]
        return keep_set, outsource_ids

    def _solve_dp_scaled(
        self,
        items: List[Dict],
        budget: int,
        target_scaled_budget: int = 5000,
        fallback_threshold: int = 5_000_000,
    ) -> Tuple[Set[str], List[str]]:
        """
        Scaled 0/1 knapsack DP to handle very large weights/budgets.

        Args:
            items: List of knapsack items
            budget: Original budget
            target_scaled_budget: Target scaled budget size
            fallback_threshold: Threshold for falling back to greedy

        Returns:
            Tuple of (keep_set, outsource_ids)
        """
        n = len(items)
        if budget <= 0 or n == 0:
            return set(), [it["id"] for it in items]

        # Choose scale factor
        scale = max(1, math.ceil(budget / max(1, target_scaled_budget)))
        scaled_budget = max(1, budget // scale)
        scaled_budget = int(scaled_budget)

        def ceil_div(a, b):
            return (a + b - 1) // b

        # Build scaled items
        scaled_items = []
        for it in items:
            w = max(1, math.ceil(float(it["weight"])))
            v = max(0, int(float(it["value"])))
            sw = max(1, ceil_div(w, scale))
            scaled_items.append(
                {
                    "id": it["id"],
                    "weight": sw,
                    "value": v,
                    "orig_weight": w,
                }
            )

        # Fallback to greedy if DP would be too large
        if n * scaled_budget > fallback_threshold:
            ranked = sorted(
                scaled_items,
                key=lambda x: x["value"] / max(1, x["weight"]),
                reverse=True,
            )
            keep, total_sw = [], 0
            for it in ranked:
                if total_sw + it["weight"] <= scaled_budget:
                    keep.append(it)
                    total_sw += it["weight"]
            keep_ids = set(it["id"] for it in keep)
            keep_ids = self._repair_for_original_budget(keep_ids, items, budget)
            outsource_ids = [
                it["id"] for it in items if it["id"] not in keep_ids
            ]
            return keep_ids, outsource_ids

        # Standard 0/1 knapsack DP on scaled instance
        dp = [0] * (scaled_budget + 1)
        choice = [[False] * (scaled_budget + 1) for _ in range(n)]

        for i, it in enumerate(scaled_items):
            w = it["weight"]
            v = it["value"]
            if w > scaled_budget:
                continue
            for b in range(scaled_budget, w, -1):
                if dp[b - w] + v > dp[b]:
                    dp[b] = dp[b - w] + v
                    choice[i][b] = True
            if dp[w] < v:
                dp[w] = v
                choice[i][w] = True

        # Reconstruct selection
        b = max(range(scaled_budget + 1), key=lambda x: dp[x])
        keep_idx = []
        for i in range(n - 1, -1, -1):
            if b < 0:
                break
            if b <= scaled_budget and choice[i][b]:
                keep_idx.append(i)
                b -= scaled_items[i]["weight"]
                if b <= 0:
                    break

        keep_ids = set(scaled_items[i]["id"] for i in keep_idx)
        keep_ids = self._repair_for_original_budget(keep_ids, items, budget)
        outsource_ids = [it["id"] for it in items if it["id"] not in keep_ids]
        return keep_ids, outsource_ids

    def _solve_random(
        self, items: List[Dict], budget: int
    ) -> Tuple[Set[str], List[str]]:
        """
        Random selection for sanity checking.
        """
        ids = [it["id"] for it in items]
        random.shuffle(ids)
        total_w = 0
        selected = set()
        for id in ids:
            it = next(it for it in items if it["id"] == id)
            w = max(1, math.ceil(float(it["weight"])))
            if total_w + w <= budget:
                selected.add(id)
                total_w += w
            if total_w >= budget:
                break
        outsource_ids = [it["id"] for it in items if it["id"] not in selected]
        return selected, outsource_ids

    def _repair_for_original_budget(
        self, keep_ids: Set[str], items: List[Dict], budget: int
    ) -> Set[str]:
        """
        Repair solution to satisfy original budget constraints.

        If the scaled solution exceeds the true budget due to ceil rounding,
        drop items with the worst value/weight ratio until satisfied.
        """
        kept = [it for it in items if it["id"] in keep_ids]
        total_w = sum(max(1, math.ceil(float(it["weight"]))) for it in kept)

        if total_w <= budget:
            return keep_ids

        # Sort by "weakness": lowest value/weight first
        kept_sorted = sorted(
            kept,
            key=lambda it: (
                (
                    float(it["value"])
                    / max(1, math.ceil(float(it["weight"])))
                )
                if float(it["weight"]) > 0
                else float("inf")
            ),
        )

        keep_ids = set(keep_ids)
        for it in kept_sorted:
            if total_w <= budget:
                break
            item_weight = max(1, math.ceil(float(it["weight"])))
            keep_ids.discard(it["id"])
            total_w -= item_weight

        return keep_ids
