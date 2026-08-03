# fiData: bug fixes, package refactor, and automation

## Context

`fiData/mystocks.ipynb` is a 4,980-line, 19-cell monolithic notebook that parses broker
exports (Schwab/Fidelity/E*Trade, 5+ format variants), computes cost basis, fetches
prices/fundamentals via yfinance, runs MPT analysis, and exports `app_data/*.json` for two
standalone viewers (`viewer.py`, `viewer_app.py`). An external review plus direct code
reading turned up three concrete bugs, duplicated broker-parsing logic (once in the main
loader, once in the snapshot-diff loader), and a hard blocker (`input()`) that prevents the
pipeline from ever running unattended.

The goal of this work is twofold: (1) make the codebase correct and maintainable — fix the
bugs, split the monolith into a small tested package — and (2) turn it into a system that
watches the portfolio for you: a headless pipeline running 3x/day on the VM with Telegram
alerts for big moves and upcoming earnings, a daily Claude-generated summary, and a deeper
weekly review (Sunday) delivered as a short Telegram digest plus a full report on the
existing self-hosted `panel` dashboard.

User decisions already made:
- `mystocks_test.ipynb` (a diverged scratch copy, not a real test suite) → **delete** once
  real fixture tests exist.
- News alerts → **out of scope for v1** (yfinance's news feed is unreliable); only big-move
  and earnings alerts ship now.
- Weekly review delivery → **Telegram summary + full report on `panel`** (see Phase 5), not
  email.

---

## Phase 1 — Bug fixes

1. **`combined.rename()` no-op** (cell 1): change to
   `combined = combined.rename(index={'BRK/B': 'BRK-B'})`. Currently the rename is discarded,
   so `BRK/B` leaks into `historical.csv` columns downstream.
2. **`result = result = (...)` typo** (cell 0, Fidelity xlsx branch): collapse to a single
   `result = (...)`. Cosmetic only.
3. **Blocking `input()` in snapshot-diff loop** (cell 5, ~line 184): remove the interactive
   prompt entirely. When a trade's price can't be resolved from the snapshot or
   `historical.csv`, unconditionally log it to `unknown_trades.csv` and continue — keep only
   the existing `except`/log path, drop the `input()` attempt. This is required before the
   headless pipeline (Phase 4) can run unattended, so do it as part of extracting this logic
   into `analytics.infer_missing_trades()` in Phase 2, not as a separate patch.

---

## Phase 2 — Refactor into a package

New layout under `fiData/`:

```
fiData/
  parsers/
    __init__.py     # detect_format(), load_positions(), load_snapshot()
    common.py        # clean_num(), is_option(), NON_EQUITY_SYMBOLS, CASH_ALIASES (single source of truth)
    schwab.py         # parse_all_accounts(), parse_single_account()
    fidelity.py        # parse_positions_csv(), parse_holdings_xlsx()
    etrade.py           # parse_account_summary()
    transactions.py      # parse_history_csv/xlsx, parse_realized_gain_csv, load_transactions(), load_realized_lots()
  enrich.py           # refresh_historical(), classify_sectors(), analyst_targets(), earnings_and_recommendations()
  analytics.py        # merge_accounts(), compute_cost_basis(), infer_missing_trades(), mpt_metrics(), efficient_frontier(), export_app_data()
  app_data_io.py       # shared load()/fmt() used by viewer.py and viewer_app.py
  telegram_alert.py     # send_telegram() — mirrors todo_list/accountability_bot.py's pattern
  alerts.py            # detect_alerts(): big moves + upcoming earnings
  ai_review.py         # daily_summary(), weekly_deep_review() — Claude API, mirrors todo_list/franklin/coach.py
  run_pipeline.py      # headless entry point (Phase 4)
  data/
    last_run_snapshot.csv   # baseline for big-move detection
    alerted_earnings.json    # dedup state for earnings alerts
  tests/
    fixtures/           # small anonymized excerpts, one per broker format
    conftest.py
    test_parsers_schwab.py
    test_parsers_fidelity.py
    test_parsers_etrade.py
    test_transactions.py
  mystocks.ipynb        # reduced to orchestration
```

### Eliminating the cell 0 / cell 5 duplication

Cell 0 (main loader) and cell 5's `_load_snapshot()` currently reimplement the same Schwab
All-Accounts / Schwab single-account / Fidelity parsing with slightly diverging constants
(`NON_EQUITY_AM` vs `NON_EQ`). Fix: both call sites use the **same** `parsers.schwab.*` /
`parsers.fidelity.*` / `parsers.etrade.*` functions and the single `NON_EQUITY_SYMBOLS`
constant in `parsers/common.py`.

```python
# parsers/__init__.py
def detect_format(filepath: str) -> str: ...
def load_positions(accounts_dir: str, exclude: set[str] = ...) -> dict[str, pd.DataFrame]:
    """Full loader, replaces cell 0's loop. Columns: Quantity, Current_Price, Market_Value."""
def load_snapshot(filepath: str) -> dict[str, pd.DataFrame]:
    """Replaces cell 5's _load_snapshot(). Calls the SAME parse_* functions as
    load_positions, then projects down to [Quantity, Price]. No separate parsing path."""
```

`parsers/transactions.py` gets the same treatment for the three transaction-history formats
used by cell 2 (Fidelity CSV history, E*Trade xlsx history, Schwab realized-gain lot CSV).

### `analytics.py` — cost basis and REINVESTED handling

- `compute_cost_basis(...)` adds a new **`Cost_Basis_Source`** column
  (`'transaction_history' | 'brokerage_fallback' | 'default_cutoff'`) so it's visible which
  rows are distorted by the `DEFAULT_BUY` (Jan 2023) fudge for pre-cutoff positions.
- `parsers/transactions.py`'s `_parse_action()` now returns a distinct `'REINVEST'` action
  (currently folded into `'BUY'`). `compute_cost_basis` still includes REINVEST in cost-basis
  lots (it's real tax basis), but a new `Capital_Deployed` aggregate filters to `Action ==
  'BUY'` only, with `Dividends_Reinvested` reported separately — this changes a
  currently-printed number, so print both old/new framing clearly in the notebook output on
  first run rather than silently shifting a number.

### `app_data_io.py`

Diff `viewer.py`'s and `viewer_app.py`'s existing `load()`/`fmt()` implementations first
(don't assume they're identical — viewer_app.py is Tkinter and may have GUI-specific
formatting inlined), then extract the common parts into `app_data_io.py` and have both
viewers import from it instead of each defining their own copy.

### Fixture-based parser tests

Copy small (3–5 row), header-preserving, anonymized excerpts into `tests/fixtures/` from:

| Fixture | Source |
|---|---|
| `schwab_all_accounts.csv` | `accounts/All-Accounts-Positions-2026-06-18-132659.csv` |
| `schwab_single_account.csv` | one of `accounts/Designated Bene Individual-Positions-*.csv` |
| `fidelity_positions.csv` | `accounts/Portfolio_Positions_Jun-18-2026.csv` |
| `fidelity_holdings.xlsx` | `accounts/Holdings.xlsx` |
| `etrade_account_summary.csv` | `accounts/PortfolioDownload(3).csv` |
| `fidelity_history.csv` | `buysell/Accounts_History.csv` |
| `etrade_history.xlsx` | `buysell/History(1).xlsx` |
| `schwab_realized_gain.csv` | `buysell/All_Accounts_GainLoss_Realized_Details_20260618-132720.csv` |

Each test module asserts: `detect_format()` returns the right label, the parsed result has
the right keys/columns (`Quantity`, `Current_Price`, `Market_Value`, index `Symbol`), cash
rows get relabeled to `'cash'`, `NON_EQUITY_SYMBOLS` rows are dropped, and row
count/known values match the fixture exactly (regression pin). This is the direct fix for
the reviewer's top operational risk: broker export formats change silently.

### Notebook reduction

Cells 0, 1, 2, 5, 4, 8, 9, 10, 12 shrink to thin calls into the new package (import + call +
keep the human-readable `print()`s), e.g. cell 0 goes from 247 lines to ~10. MPT cells
(15/16) keep their `matplotlib`/`plt.show()` in the notebook but call
`analytics.mpt_metrics()` / `analytics.efficient_frontier()` for the numeric work, so
`run_pipeline.py` can reuse the same functions headlessly without a display backend.

### Cleanup

Delete `mystocks_test.ipynb` once the fixture tests pass (confirmed by user — it's a
diverged scratch copy, not a real suite).

---

## Phase 3 — Config additions

`requirements.txt`: add `anthropic`, `requests`, `pytest` (already present in the local
`p312` conda env, but need pinning here so `env_sync.py install_reqs` sets up the VM venv
correctly).

New env vars needed in master `.env` (then synced to `fiData/.env`):
- `FIDATA_THREAD_ID` — new Telegram topic ID. **User needs to create a new topic in the
  existing `PINGER_CHANNEL_ID` forum group** and supply its id.
- Confirm `PING_BOT_ID`, `PINGER_CHANNEL_ID`, `ANTHROPIC_API_KEY` (already global in master
  `.env`) actually propagate into `fiData/.env` via `env_sync.py sync` — they're currently
  absent from `fiData/.env`, so verify `sync_cmd()`'s global-key selection picks them up
  rather than assuming it.

`env_sync.py` change: add `"fiData"` to `_VM_PROJECTS` (line 435) — `push_env`/`git_pull`/
`install_reqs` are already generic per-project, no bespoke command needed.

---

## Phase 4 — Headless pipeline (`fiData/run_pipeline.py`)

Depends on Phase 1 bug #3 and Phase 2's package. One-shot script, run 3x/day via systemd
timer (not a long-running daemon):

```python
def run() -> None:
    accounts = load_positions(...)
    combined = merge_accounts(accounts)
    tx_df, sold_df = load_transactions(...), load_realized_lots(...)
    inferred = infer_missing_trades(collect_snapshots(...), tx_df, hist_df, UNKNOWN_FILE)
    combined = compute_cost_basis(...)
    hist_df = refresh_historical(symbols, HIST_FILE)
    sector_df = classify_sectors(symbols, SECTOR_CSV)
    earn_df, recs_df, upgrades = earnings_and_recommendations(...)
    export_app_data(APP_DATA, accounts, combined, ...)   # keeps viewer.py/viewer_app.py working
    for msg in detect_alerts(combined, PREV_SNAPSHOT_FILE, earn_df):
        send_telegram(msg)
    save_run_baseline(combined, PREV_SNAPSHOT_FILE)
```

**Alert conditions (v1: big moves + earnings only, per user decision):**
- **Big moves**: persist `combined[['Symbol','Current_Price','Market_Value']]` to
  `data/last_run_snapshot.csv` after each run; next run flags `abs(pct_change) > 5%` since
  the last saved snapshot (config constant `BIG_MOVE_PCT`). Since this runs 3x/day, it's
  naturally "move since last check," not a daily figure — intended, not a bug.
- **Earnings**: reuse `enrich.earnings_and_recommendations()`; flag holdings with
  `Next_Earnings` within 3 days, dedup via `data/alerted_earnings.json` so three runs/day
  don't triple-send the same alert.

**Telegram delivery** (`telegram_alert.py`) mirrors `todo_list/accountability_bot.py`'s
`send()` exactly — direct `requests.post` to the Bot API using `PING_BOT_ID` +
`PINGER_CHANNEL_ID` + the new `FIDATA_THREAD_ID`. This is send-only, so it does **not** need
to join `todo_list/run_bots.py`'s shared long-poller — zero changes needed there.

---

## Phase 5 — Claude API reviews

Mirrors `todo_list/franklin/coach.py`'s pattern (`anthropic.Anthropic()` client, `system` +
`user` messages). Check current model id against the `claude-api` skill before hardcoding.

```python
# fiData/ai_review.py
def daily_summary(combined, hist_df, earn_df, alerts_today) -> str:
    """3-5 bullet points, notable moves + upcoming earnings. ~400 max_tokens."""
def weekly_deep_review(combined, sector_data, mpt_metrics, sold_df) -> dict[str, str]:
    """Structured sections: Rebalancing, Sector Drift, Tax-Loss Harvesting (unrealized
    losses where Return_% < 0, cross-checked against Cost_Basis_Source to avoid
    recommending TLH on DEFAULT_BUY-fudged rows), Watch List. ~1500-2000 max_tokens."""
```

**Delivery (per user decision):**
- **Daily summary** → Telegram only, `FIDATA_THREAD_ID`.
- **Weekly deep review** → a short Telegram digest (1-2 lines per section + a link) **plus
  the full structured report on the existing `panel` dashboard**, following `panel`'s
  established pattern:
  - `panel` is a plain Flask app (`panel/app.py`), one Blueprint per feature
    (`*_routes.py` + a same-named template), registered with a try/except so one broken
    feature doesn't take the panel down. No auth — access is Tailscale-network-only, per
    `app.py`'s existing docstring, so no login code needed.
  - Add `panel/fidata_routes.py` (Blueprint `fidata_bp`, prefix `/fidata`): resolves
    fiData's path VM-first (`~/apps/fiData`) then laptop-fallback
    (`~/Documents/fiData`), reads the latest weekly review + `combined`/`sector_data` either
    from a small JSON the weekly job writes (e.g. `fiData/data/weekly_review_<date>.json`)
    or by importing `app_data_io`. Add a `?week=` query-param nav modeled on
    `wp_routes.py`'s `_week_date_range()` pattern for browsing past weekly reports.
  - Add `panel/templates/fidata_dashboard.html`: same structure as `wp_dashboard.html` —
    header card, holdings/performance table, a stats card, and a narrative "synthesis" card
    per section (`Rebalancing`/`Sector Drift`/`Tax-Loss Harvesting`/`Watch List`), styled
    with the same inline dark-theme CSS block (`#0f172a`/`#1e293b`/`#38bdf8`) other panel
    templates use — no shared base template exists in panel, each page is standalone.
  - Register in `app.py` the same way as the other blueprints:
    ```python
    try:
        from fidata_routes import fidata_bp
        app.register_blueprint(fidata_bp, url_prefix="/fidata")
    except Exception as _e:
        logging.getLogger(__name__).warning("fiData blueprint unavailable: %s", _e)
    ```
  - **No new systemd unit or deploy command needed** — this reuses the existing `app-panel`
    service and `env_sync.py`'s `deploy_panel` command (which globs `panel/*.py` and
    `panel/templates/*.html`, so the new files are picked up automatically).
  - The weekly job (`run_pipeline.py`'s Sunday variant, see Phase 6) writes the full review
    JSON to `fiData/data/`, then sends a short Telegram digest with a link to
    `http://<tailscale-ip>:9000/fidata`.

---

## Phase 6 — VM deployment

No long-running daemon needed for the pipeline/review jobs (all one-shot scripts) — use
systemd timer + oneshot service pairs, consistent with how `app-todo`/`app-adhd` are managed
as systemd units already:

- `fidata-pipeline.service` (`python run_pipeline.py`) + `fidata-pipeline.timer`
  (`OnCalendar=*-*-* 07,13,19:00:00`, `America/Los_Angeles`, 3x/day).
- `fidata-daily-review.service` + `.timer` (`OnCalendar=*-*-* 16:15:00`, after a pipeline
  run so data is fresh).
- `fidata-weekly-review.service` + `.timer` (`OnCalendar=Sun 14:00:00`).

Venv: `~/apps/fiData/venv/` on the VM, matching the `todo_list`/`adhd-bot` convention.

Deploy flow: `git push` locally → `python env_sync.py git_pull fiData` → `python env_sync.py
install_reqs fiData` → `python env_sync.py push_env fiData` (propagates `FIDATA_THREAD_ID`
etc.) → `python env_sync.py deploy_panel` (for the new blueprint/template) →
`sudo systemctl daemon-reload && sudo systemctl enable --now fidata-pipeline.timer
fidata-daily-review.timer fidata-weekly-review.timer`.

---

## Verification

1. `pytest fiData/tests/` — all fixture parser tests green.
2. Run the refactored `mystocks.ipynb` end-to-end against real `accounts/`/`buysell/`/
   `past/`; diff `app_data/*.json` against a pre-refactor baseline to confirm the three bug
   fixes (plus the flagged `Cost_Basis_Source`/`REINVEST` changes) are the only deltas.
3. Launch `viewer.py` and `viewer_app.py` against the refactored `app_data/*.json`, click
   through each view — no `KeyError`/missing-column regressions.
4. Run `python run_pipeline.py` manually once: zero prompts/blocking calls, a Telegram
   message lands in the test topic, `data/last_run_snapshot.csv` and
   `data/alerted_earnings.json` are written correctly.
5. Deploy the systemd timers with a short test `OnCalendar` first (e.g. every 5 min),
   confirm firing via `journalctl -u fidata-pipeline.service`, then switch to the real
   schedule.
6. Manually trigger `daily_summary()` and `weekly_deep_review()` once each; inspect output
   for reasonableness; load `http://<tailscale-ip>:9000/fidata` and confirm the weekly
   report renders correctly, including past-week navigation.

---

## Implementation status (2026-08-02)

Phases 1-5 are implemented and verified locally:

- **Bug fixes**: all three applied (`analytics.merge_accounts` rename fix,
  `parsers/fidelity.py` typo cleanup, `analytics.infer_missing_trades` has
  no `input()` call).
- **Package**: `parsers/`, `analytics.py`, `enrich.py`, `app_data_io.py`
  built; `viewer.py`/`viewer_app.py` now import the shared helpers.
- **Tests**: 14 fixture-based parser tests in `tests/`, all passing
  (`pytest tests/`). Found and fixed one real latent bug in the process —
  `_parse_action` was checking for the substring `'REINVESTED'`, which
  wouldn't have matched Fidelity's actual `'REINVESTMENT ...'` action text;
  now checks `'REINVEST'`.
- **Notebook**: `mystocks.ipynb` reduced to orchestration calls; ran
  end-to-end against real data (all 19 cells, including MPT/efficient
  frontier/correlation heatmap) with zero errors. Diffed `app_data/*.json`
  against a pre-refactor baseline: only the intended deltas showed up
  (`BRK/B`→`BRK-B`, new `Cost_Basis_Source` column) — plus a bonus fix,
  `sectors.json`'s `by_cap`/`by_vol` were previously always empty because
  the old cell 8 computed `Cap_Tier`/`Vol_Tier` on a local copy and never
  wrote them back into `combined`; the new `enrich.classify_sectors` fixes
  that as a side effect.
- **`mystocks_test.ipynb`**: deleted (was a diverged scratch copy, not a
  real test suite).
- **Headless pipeline**: `run_pipeline.py`, `alerts.py`, `telegram_alert.py`
  built and import cleanly. `alerts.py` implements only big-move + earnings
  alerts (news alerts trimmed from v1 per your decision).
- **Claude reviews**: `ai_review.py` (`daily_summary`, `weekly_deep_review`),
  `run_daily_review.py`, `run_weekly_review.py` built, mirroring
  `todo_list/franklin/coach.py`'s client pattern.
- **panel integration**: `panel/fidata_routes.py` +
  `panel/templates/fidata_dashboard.html` added and registered in
  `panel/app.py` (both the blueprint and the dashboards list); smoke-tested
  with Flask's test client, including week-to-week navigation.
- **Config**: `requirements.txt` updated (`anthropic`, `requests`, `pytest`
  pinned to the versions installed in the `p312` env); `env_sync.py`'s
  `_VM_PROJECTS` now includes `"fiData"`; master `.env` has a new
  `FIDATA_THREAD_ID=` placeholder under the existing `# fiData` section.
- **systemd units**: reference unit/timer files added under `fiData/systemd/`
  (`fidata-pipeline`, `fidata-daily-review`, `fidata-weekly-review`) — not
  yet installed on the VM.

### What's left — needs you, not more code

1. **Create the Telegram topic.** Open the `PINGER_CHANNEL_ID` forum group,
   create a new topic for fiData, and fill in its numeric thread id as
   `FIDATA_THREAD_ID` in `/home/ai1/Documents/.env` (currently blank — until
   it's set, `telegram_alert.send_telegram()` just logs to stdout instead of
   sending). Then run `python env_sync.py sync` to propagate it.
2. **Deploy to the VM**: `git push` → `python env_sync.py git_pull fiData` →
   `python env_sync.py install_reqs fiData` → `python env_sync.py push_env fiData`
   → `python env_sync.py deploy_panel` (for the new blueprint/template) →
   copy `fiData/systemd/*.service` and `*.timer` to `~/.config/systemd/user/`
   (or `/etc/systemd/system/` with root paths adjusted) on the VM, then
   `systemctl daemon-reload && systemctl enable --now fidata-pipeline.timer
   fidata-daily-review.timer fidata-weekly-review.timer`.
3. **`ANTHROPIC_API_KEY` / model check**: `ai_review.py` defaults to
   `claude-sonnet-4-6` (matching `todo_list/franklin/coach.py`'s existing
   convention) with an override via `FIDATA_COACH_MODEL` — confirm that's
   still the model you want before the first real weekly review runs.
4. Run the two verification steps that need live Telegram/Claude
   credentials, which weren't exercised in this session: an actual
   `send_telegram()` round-trip once `FIDATA_THREAD_ID` is set, and one real
   `daily_summary()`/`weekly_deep_review()` call against the live
   `ANTHROPIC_API_KEY`.
