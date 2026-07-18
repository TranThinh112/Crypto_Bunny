from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild historical market-pattern snapshots from exported OHLCV JSONL.")
    parser.add_argument("input", help="JSONL file containing MarketAnalysisRequest payloads")
    parser.parse_args()
    raise SystemExit("History rebuild is intentionally explicit; stream payloads through run_analysis.py after reviewing data source.")


if __name__ == "__main__":
    raise SystemExit(main())
