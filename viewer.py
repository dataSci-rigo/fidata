#!/usr/bin/env python3
"""
Portfolio Viewer — reads exported JSON from mystocks.ipynb (app_data/)
Run:  python viewer.py
"""

import os
import sys

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML

from app_data_io import load, fmt, pct_color

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA = os.path.join(DATA_DIR, "app_data")

console = Console()


def go_back():
    console.print("\n[dim]Press Enter to return to menu…[/dim]")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass


# ── Views ─────────────────────────────────────────────────────────────────────

def view_accounts():
    data = load("accounts.json")
    if not data:
        console.print("[red]accounts.json not found — run mystocks.ipynb first.[/red]")
        go_back(); return

    acct_ids = sorted(data.keys())
    while True:
        console.clear()
        console.print(Panel("[bold cyan]Accounts[/bold cyan]", expand=False))
        for i, acct in enumerate(acct_ids, 1):
            console.print(f"  [bold]{i}.[/bold] Account {acct}")
        console.print("\n  [bold]B.[/bold] Back")

        choice = prompt(HTML("<ansiyellow>Select account: </ansiyellow>")).strip().lower()
        if choice == "b":
            return
        try:
            idx = int(choice) - 1
            acct = acct_ids[idx]
        except (ValueError, IndexError):
            continue

        rows = data[acct]
        console.clear()
        t = Table(title=f"Account {acct}", box=box.SIMPLE_HEAVY, show_lines=False)
        # Determine columns from first row
        if not rows:
            console.print("[yellow]No positions.[/yellow]"); go_back(); continue

        keys = list(rows[0].keys())
        sym_col = next((k for k in keys if k in ("Symbol", "Security ID")), keys[0])
        num_cols = [k for k in keys if k != sym_col]

        t.add_column(sym_col, style="bold cyan", no_wrap=True)
        for k in num_cols:
            t.add_column(k, justify="right")

        for row in rows:
            vals = []
            for k in num_cols:
                v = row.get(k)
                if k in ("Quantity",):
                    vals.append(fmt(v, "int") if isinstance(v, float) and v == int(v) else fmt(v))
                elif k in ("Current_Price", "Current Price"):
                    vals.append(fmt(v, "$"))
                elif k in ("Market_Value", "Market Value"):
                    vals.append(fmt(v, "$big"))
                else:
                    vals.append(fmt(v))
            t.add_row(str(row.get(sym_col, "?")), *vals)

        console.print(t)
        go_back()


def view_combined():
    data = load("combined.json")
    if not data:
        console.print("[red]combined.json not found.[/red]"); go_back(); return

    console.clear()
    t = Table(title="Full Portfolio", box=box.SIMPLE_HEAVY, show_lines=False)
    cols = [
        ("Symbol",        "bold cyan", "str",  False),
        ("Quantity",      "right",     "int",  False),
        ("Current_Price", "right",     "$",    False),
        ("Market_Value",  "right",     "$big", False),
        ("Trailing_PE",   "right",     "x",    False),
        ("Forward_PE",    "right",     "x",    False),
        ("Ann_Vol",       "right",     "%",    False),
        ("Gain_3m",       "right",     "pct",  False),
        ("Gain_6m",       "right",     "pct",  False),
        ("Gain_1yr",      "right",     "pct",  False),
    ]
    present = {r.get("Symbol", r.get("Security ID")) for r in data}
    for name, style, kind, _ in cols:
        t.add_column(name, style=style if style != "right" else "", justify="right" if style == "right" else "left")

    for row in sorted(data, key=lambda r: r.get("Symbol") or ""):
        cells = []
        for name, style, kind, _ in cols:
            v = row.get(name)
            if kind == "pct":
                cells.append(pct_color(v))
            elif kind == "int":
                cells.append(fmt(v, "int") if isinstance(v, (int, float)) and v == v else "—")
            else:
                cells.append(fmt(v, kind))
        t.add_row(*cells)

    console.print(t)
    go_back()


def view_sectors():
    data = load("sectors.json")
    if not data:
        console.print("[red]sectors.json not found.[/red]"); go_back(); return

    options = [
        ("by_gics", "By GICS Sector"),
        ("by_cap",  "By Market-Cap Tier"),
        ("by_vol",  "By Volatility Tier"),
    ]
    while True:
        console.clear()
        console.print(Panel("[bold cyan]Sector / Tier Summaries[/bold cyan]", expand=False))
        for i, (_, label) in enumerate(options, 1):
            console.print(f"  [bold]{i}.[/bold] {label}")
        console.print("\n  [bold]B.[/bold] Back")

        choice = prompt(HTML("<ansiyellow>Select view: </ansiyellow>")).strip().lower()
        if choice == "b":
            return
        try:
            key, label = options[int(choice) - 1]
        except (ValueError, IndexError):
            continue

        rows = data.get(key, [])
        console.clear()
        if not rows:
            console.print("[yellow]No data.[/yellow]"); go_back(); continue

        t = Table(title=label, box=box.SIMPLE_HEAVY, show_lines=False)
        group_col = list(rows[0].keys())[0]
        t.add_column(group_col, style="bold cyan")
        t.add_column("Total Market Value", justify="right")
        t.add_column("Gain 3m",  justify="right")
        t.add_column("Gain 6m",  justify="right")
        t.add_column("Gain 1yr", justify="right")

        for row in sorted(rows, key=lambda r: -(r.get("Total_Market_Value") or 0)):
            t.add_row(
                str(row.get(group_col, "?")),
                fmt(row.get("Total_Market_Value"), "$big"),
                pct_color(row.get("Gain_3m")),
                pct_color(row.get("Gain_6m")),
                pct_color(row.get("Gain_1yr")),
            )
        console.print(t)
        go_back()


def view_flags():
    data = load("flags.json")
    if not data:
        console.print("[red]flags.json not found.[/red]"); go_back(); return

    options = [
        ("high_trailing_pe", "⚠  Highest Trailing P/E"),
        ("high_forward_pe",  "⚠  Highest Forward P/E"),
        ("worst_3m",         "⚠  Worst 3-Month Performance"),
        ("low_forward_pe",   "✅  Lowest Forward P/E"),
        ("best_3m",          "✅  Best 3-Month Performance"),
    ]
    while True:
        console.clear()
        console.print(Panel("[bold cyan]Flags & Rankings[/bold cyan]", expand=False))
        for i, (_, label) in enumerate(options, 1):
            console.print(f"  [bold]{i}.[/bold] {label}")
        console.print("\n  [bold]B.[/bold] Back")

        choice = prompt(HTML("<ansiyellow>Select flag: </ansiyellow>")).strip().lower()
        if choice == "b":
            return
        try:
            key, label = options[int(choice) - 1]
        except (ValueError, IndexError):
            continue

        rows = data.get(key, [])
        console.clear()
        if not rows:
            console.print("[yellow]No data.[/yellow]"); go_back(); continue

        t = Table(title=label, box=box.SIMPLE_HEAVY, show_lines=False)
        t.add_column("Symbol", style="bold cyan")
        col_fmts = {
            "Trailing_PE":   ("Trailing PE",   "x"),
            "Forward_PE":    ("Forward PE",    "x"),
            "Current_Price": ("Price",         "$"),
            "Market_Value":  ("Mkt Value",     "$big"),
            "Gain_3m":       ("Gain 3m",       "pct"),
            "Gain_6m":       ("Gain 6m",       "pct"),
            "Gain_1yr":      ("Gain 1yr",      "pct"),
            "Sharpe_3m":     ("Sharpe 3m",     "str"),
            "Target_Mean":   ("Target Mean",   "$"),
        }
        present = [k for k in col_fmts if k in (rows[0] if rows else {})]
        for k in present:
            t.add_column(col_fmts[k][0], justify="right")

        for row in rows:
            cells = []
            for k in present:
                v = row.get(k)
                kind = col_fmts[k][1]
                cells.append(pct_color(v) if kind == "pct" else fmt(v, kind))
            t.add_row(str(row.get("Symbol", "?")), *cells)

        console.print(t)
        go_back()


def view_targets():
    data = load("targets.json")
    if not data:
        console.print("[red]targets.json not found.[/red]"); go_back(); return

    options = [
        ("overvalued",  "⚠  Analyst Median Below Current Price"),
        ("most_upside", "✅  Most Upside to Analyst Median (top 10)"),
        ("tightest",    "✅  Tightest Analyst Consensus (top 10)"),
    ]
    while True:
        console.clear()
        console.print(Panel("[bold cyan]Analyst Price Targets[/bold cyan]", expand=False))
        for i, (_, label) in enumerate(options, 1):
            console.print(f"  [bold]{i}.[/bold] {label}")
        console.print("\n  [bold]B.[/bold] Back")

        choice = prompt(HTML("<ansiyellow>Select view: </ansiyellow>")).strip().lower()
        if choice == "b":
            return
        try:
            key, label = options[int(choice) - 1]
        except (ValueError, IndexError):
            continue

        rows = data.get(key, [])
        console.clear()
        if not rows:
            console.print("[yellow]No data.[/yellow]"); go_back(); continue

        t = Table(title=label, box=box.SIMPLE_HEAVY, show_lines=False)
        t.add_column("Symbol",       style="bold cyan")
        t.add_column("Price",        justify="right")
        t.add_column("Target Med",   justify="right")
        t.add_column("Upside %",     justify="right")
        t.add_column("Target High",  justify="right")
        t.add_column("Target Low",   justify="right")
        t.add_column("Spread %",     justify="right")
        t.add_column("# Analysts",   justify="right")

        for row in rows:
            upside = row.get("Target_Upside")
            t.add_row(
                str(row.get("Symbol", "?")),
                fmt(row.get("Current_Price"), "$"),
                fmt(row.get("Target_Median"),  "$"),
                pct_color(upside),
                fmt(row.get("Target_High"), "$"),
                fmt(row.get("Target_Low"),  "$"),
                fmt(row.get("Target_Spread"), "%") if row.get("Target_Spread") else "—",
                fmt(row.get("Num_Analysts"), "int"),
            )
        console.print(t)
        go_back()


def view_earnings():
    data = load("earnings.json")
    console.clear()
    if not data:
        console.print("[yellow]No upcoming earnings data found.[/yellow]"); go_back(); return

    t = Table(title="Upcoming Earnings", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Symbol",       style="bold cyan")
    t.add_column("Earnings Date", justify="left")
    t.add_column("EPS Est",      justify="right")
    t.add_column("Rev High",     justify="right")
    t.add_column("Rev Low",      justify="right")

    for row in data:
        date_str = (row.get("Next_Earnings") or "")[:10]
        t.add_row(
            str(row.get("Symbol", "?")),
            date_str,
            fmt(row.get("EPS_Est")),
            fmt(row.get("Rev_Est_High"), "$big") if row.get("Rev_Est_High") else "—",
            fmt(row.get("Rev_Est_Low"),  "$big") if row.get("Rev_Est_Low")  else "—",
        )
    console.print(t)
    go_back()


def view_recommendations():
    data = load("recommendations.json")
    console.clear()
    if not data:
        console.print("[yellow]No recommendations data found.[/yellow]"); go_back(); return

    t = Table(title="Analyst Recommendations (current month)", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Symbol",      style="bold cyan")
    t.add_column("Consensus",   style="bold")
    t.add_column("Strong Buy",  justify="right", style="green")
    t.add_column("Buy",         justify="right", style="green")
    t.add_column("Hold",        justify="right", style="yellow")
    t.add_column("Sell",        justify="right", style="red")
    t.add_column("Strong Sell", justify="right", style="red")

    consensus_color = {
        "Strong Buy": "bold green", "Buy": "green",
        "Hold": "yellow", "Sell": "red", "Strong Sell": "bold red", "Mixed": "white"
    }
    for row in sorted(data, key=lambda r: -(r.get("Strong_Buy", 0) + r.get("Buy", 0))):
        c = row.get("Consensus", "Mixed")
        col = consensus_color.get(c, "white")
        t.add_row(
            str(row.get("Symbol", "?")),
            f"[{col}]{c}[/{col}]",
            fmt(row.get("Strong_Buy"), "int"),
            fmt(row.get("Buy"),        "int"),
            fmt(row.get("Hold"),       "int"),
            fmt(row.get("Sell"),       "int"),
            fmt(row.get("Strong_Sell"), "int"),
        )
    console.print(t)
    go_back()


def view_upgrades():
    data = load("upgrades.json")
    console.clear()
    if not data:
        console.print("[yellow]No upgrades/downgrades data found.[/yellow]"); go_back(); return

    t = Table(title="Upgrades / Downgrades (last 90 days)", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Symbol",   style="bold cyan")
    t.add_column("Date",     justify="left")
    t.add_column("Firm",     justify="left")
    t.add_column("Action",   justify="left")
    t.add_column("To Grade", justify="left")
    t.add_column("From Grade", justify="left")
    t.add_column("Target",   justify="right")

    action_color = {"up": "green", "down": "red", "init": "cyan", "reit": "yellow"}
    for row in data:
        action = str(row.get("Action", "")).lower()
        color  = next((v for k, v in action_color.items() if k in action), "white")
        date_str = str(row.get("GradeDate") or "")[:10]
        t.add_row(
            str(row.get("Symbol", "?")),
            date_str,
            str(row.get("Firm", "?")),
            f"[{color}]{row.get('Action','?')}[/{color}]",
            str(row.get("ToGrade",   "?")),
            str(row.get("FromGrade", "?")),
            fmt(row.get("currentPriceTarget"), "$"),
        )
    console.print(t)
    go_back()


# ── Splash / main menu ────────────────────────────────────────────────────────

MENU = [
    ("Portfolio Overview (combined)",       view_combined),
    ("Individual Accounts",                  view_accounts),
    ("Sector / Cap / Vol Summaries",         view_sectors),
    ("Flags  (high PE · worst/best 3m)",     view_flags),
    ("Analyst Price Targets",                view_targets),
    ("Upcoming Earnings",                    view_earnings),
    ("Buy / Sell / Hold Recommendations",    view_recommendations),
    ("Upgrades & Downgrades (90 days)",      view_upgrades),
]


def splash():
    console.clear()
    console.print(Panel(
        Text("📈  Portfolio Viewer", justify="center", style="bold white on dark_blue"),
        expand=False
    ))
    console.print(f"\n  Data directory: [dim]{APP_DATA}[/dim]\n")
    for i, (label, _) in enumerate(MENU, 1):
        console.print(f"  [bold cyan]{i}.[/bold cyan] {label}")
    console.print("\n  [bold cyan]Q.[/bold cyan] Quit\n")


def main():
    if not os.path.isdir(APP_DATA):
        console.print(
            f"[red]app_data/ not found at {APP_DATA}\n"
            "Run all cells in mystocks.ipynb first, then try again.[/red]"
        )
        sys.exit(1)

    while True:
        splash()
        try:
            choice = prompt(HTML("<ansiyellow>Select: </ansiyellow>")).strip().lower()
        except (KeyboardInterrupt, EOFError):
            break

        if choice == "q":
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(MENU):
                MENU[idx][1]()
        except (ValueError, IndexError):
            pass

    console.print("\n[dim]Goodbye.[/dim]\n")


if __name__ == "__main__":
    main()
