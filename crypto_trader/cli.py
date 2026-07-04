from __future__ import annotations

import argparse
import json
import time
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import load_config, project_path
from .deploy_check import check_deploy, format_deploy_check
from .engine import run_once
from .models import Decision, to_jsonable


console = Console()


def _print_decision(decision: Decision, config: dict[str, Any], as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(to_jsonable(decision), ensure_ascii=False))
        return

    console.print(f"[bold]Mode:[/bold] {decision.mode}")
    console.print(f"[bold]Action:[/bold] {decision.action}")
    if decision.selected:
        selected = decision.selected
        console.print(
            f"[bold]Selected:[/bold] {selected.symbol} {selected.side.upper()} "
            f"confidence={selected.confidence:.2f} RR={selected.risk_reward:.2f}"
        )
        console.print(
            f"Entry={selected.entry} SL={selected.stop_loss} TP={selected.take_profit} "
            f"Qty={selected.quantity} Risk~{selected.planned_risk_usdt:.4f} USDT"
        )
        for reason in selected.reasons[:6]:
            console.print(f"  + {reason}")
    else:
        console.print("[yellow]No trade selected.[/yellow]")

    if decision.risk_check.reasons:
        console.print("[bold red]Risk blocks:[/bold red]")
        for reason in decision.risk_check.reasons:
            console.print(f"  - {reason}")
    if decision.risk_check.warnings:
        console.print("[bold yellow]Warnings:[/bold yellow]")
        for warning in decision.risk_check.warnings[:8]:
            console.print(f"  - {warning}")
    if decision.execution:
        console.print(f"[bold]Execution:[/bold] {decision.execution.message}")

    table = Table(title="Top Candidates")
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Conf", justify="right")
    table.add_column("RR", justify="right")
    table.add_column("News", justify="right")
    table.add_column("Spread %", justify="right")
    for candidate in decision.candidates[:8]:
        table.add_row(
            candidate.symbol,
            candidate.side,
            f"{candidate.confidence:.2f}",
            f"{candidate.risk_reward:.2f}",
            f"{candidate.news_score:+.2f}/{candidate.news_count}",
            "" if candidate.spread_pct is None else f"{candidate.spread_pct:.4f}",
        )
    console.print(table)

    report = project_path(config, config.get("report_path", "reports/latest_decision.json"))
    console.print(f"[dim]Report written to {report}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto news analyzer and OKX one-trade bot")
    parser.add_argument(
        "command",
        choices=["analyze", "trade", "run", "ui", "deploy-check"],
        help="analyze, trade, run continuously, start the web UI, or check deploy env",
    )
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--interval", type=int, default=None, help="Loop interval in seconds for run command")
    parser.add_argument("--host", default="127.0.0.1", help="Host for ui command")
    parser.add_argument("--port", type=int, default=8000, help="Port for ui command")
    parser.add_argument("--json", action="store_true", help="Print full JSON decision")
    parser.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="For deploy-check only: validate setup files without requiring secret values yet",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.command == "deploy-check":
        ok, errors, warnings = check_deploy(args.config, require_secrets=not args.allow_missing_secrets)
        console.print(format_deploy_check(ok, errors, warnings))
        raise SystemExit(0 if ok else 1)

    if args.command == "ui":
        import uvicorn

        from .ui import create_app

        app = create_app(args.config)
        console.print(f"[bold]UI:[/bold] http://{args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return

    if args.command == "run":
        interval = args.interval or int(config.get("runtime", {}).get("interval_seconds", 900))
        console.print(f"[bold]Running bot loop every {interval} seconds. Press Ctrl+C to stop.[/bold]")
        try:
            while True:
                decision = run_once(config, execute=True)
                _print_decision(decision, config, args.json)
                console.print(f"[dim]Sleeping {interval} seconds...[/dim]")
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("Stopped.")
        return

    execute = args.command == "trade"
    decision = run_once(config, execute=execute)
    _print_decision(decision, config, args.json)


if __name__ == "__main__":
    main()
