from __future__ import annotations

import importlib.util


def main() -> int:
    for module in ("talib", "patternpy", "pytrendline", "tradingpatterns"):
        print(f"{module}: {'installed' if importlib.util.find_spec(module) else 'not_installed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
