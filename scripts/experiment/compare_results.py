#!/usr/bin/env python3
"""Compare results from multiple simulation runs.

Usage:
    python scripts/experiment/compare_results.py results/experiment/*.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Dict


def load_results(filepaths: List[str]) -> List[Dict]:
    """Load results from JSON files.

    Args:
        filepaths: List of result file paths

    Returns:
        List of result dictionaries
    """
    results = []
    for filepath in filepaths:
        with open(filepath, 'r') as f:
            data = json.load(f)
            data['_filepath'] = filepath
            results.append(data)
    return results


def print_comparison(results: List[Dict]) -> None:
    """Print comparison table.

    Args:
        results: List of result dictionaries
    """
    if not results:
        print("No results to compare")
        return

    print("\n" + "="*80)
    print("SIMULATION RESULTS COMPARISON")
    print("="*80 + "\n")

    # Header
    print(f"{'File':<30} {'Total Cost':>12} {'Sub Cost':>12} {'API Cost':>12} "
          f"{'Quota Util':>12} {'Runtime':>10}")
    print("-" * 80)

    # Rows
    for result in results:
        filename = Path(result['_filepath']).name
        total_cost = result['costs']['total']
        sub_cost = result['costs']['subscription']
        api_cost = result['costs']['api']
        quota_util = result['quota_utilization']
        runtime = result['runtime_seconds']

        print(f"{filename:<30} ${total_cost:>11.2f} ${sub_cost:>11.2f} "
              f"${api_cost:>11.2f} {quota_util*100:>11.1f}% {runtime:>9.2f}s")

    print("\n" + "="*80 + "\n")

    # Summary statistics
    if len(results) > 1:
        costs = [r['costs']['total'] for r in results]
        min_cost = min(costs)
        max_cost = max(costs)
        avg_cost = sum(costs) / len(costs)

        print("Summary:")
        print(f"  - Min cost:  ${min_cost:.2f}")
        print(f"  - Max cost:  ${max_cost:.2f}")
        print(f"  - Avg cost:  ${avg_cost:.2f}")
        print(f"  - Savings:   ${max_cost - min_cost:.2f} "
              f"({(max_cost - min_cost) / max_cost * 100:.1f}%)")
        print()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Compare simulation results"
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Result JSON files to compare"
    )

    args = parser.parse_args()

    try:
        results = load_results(args.files)

        # Sort by total cost
        results.sort(key=lambda r: r['costs']['total'])

        print_comparison(results)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
