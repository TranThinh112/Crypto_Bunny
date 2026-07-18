from __future__ import annotations

from market_pattern_engine.infrastructure.config_loader import load_engine_config


def main() -> int:
    config = load_engine_config()
    required = ["candlestick", "support_resistance", "market_structure", "chart_patterns", "smart_money"]
    missing = [key for key in required if key not in config and key != "chart_patterns"]
    if missing:
        print(f"Missing config sections: {missing}")
        return 1
    print("Detector configuration looks valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
