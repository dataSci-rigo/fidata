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
  (`fidata-pipeline`, `fidata-daily-review`, `fidata-weekly-review`) — paths
  fixed to `~/apps/fidata` (lowercase, matching the actual VM/GitHub repo
  directory — see "Naming" below). Not yet installed on the VM.

### Update (2026-08-03/04): Telegram bot, VM naming, panel job controls

- **Dedicated Telegram bot**: rather than sharing `PING_BOT_ID`'s forum
  topics, fiData got its own bot (`FI_BOT_ID`, added to master `.env`'s
  `# fiData` section alongside the pre-existing `fi_bot` value).
  `telegram_alert.py` now DMs the existing global `OWNER_CHAT_ID` directly —
  no topic/thread setup needed. One-time manual step: message the bot once
  (e.g. `/start`) so it has a chat to reply into.
- **Naming**: the VM/GitHub repo directory is lowercase `fidata`, while the
  local checkout is `fiData`. `env_sync.py`'s `_VM_PROJECTS` uses `"fidata"`
  with a `_LOCAL_PATH_OVERRIDE` entry mapping it back to `DOCS / "fiData"`
  (same pattern already used for `fridge-recipes`/`wpi`). Get this wrong and
  `push_env`/`install_reqs`/`git_pull` fail confusingly.
- **Panel job controls**: `/fidata` now has a Jobs card (Run Now / Enable /
  Disable per systemd unit, live status) since the 3 fiData jobs are oneshot
  timers, not one always-on service — panel's generic Start/Stop/Restart
  buttons don't fit that shape. See `panel/fidata_routes.py`'s `JOBS`/
  `_job_status()`/`/api/status`/`/api/action`.

### Update (2026-08-04): env health check, positions dashboard, broker file sync

- **`env_sync.py check`**: validates env-key blanks/drift (local) and VM
  venv package drift vs each project's `requirements.txt` (one SSH round
  trip). Run this after any master `.env` edit or before a deploy — it would
  have caught the blank-thread-id issue from the first Telegram setup.
- **`env_sync.py push_broker_files`**: manual command to upload
  `accounts/`/`buysell/`/`past/` from this laptop to the VM after
  downloading fresh broker exports. Workflow: download exports from your
  brokers → drop the files into `fiData/accounts/` (or `buysell/`) locally →
  `python env_sync.py push_broker_files` → the next `fidata-pipeline` run
  (scheduled, or a manual "Run Now" from the `/fidata` panel Jobs card)
  picks them up. Plain recursive overwrite, no merge logic — these are files
  you drop in, not shared mutable state.
- **Positions/analytics dashboard** (`/positions` on panel, separate from
  `/fidata`'s Sunday narrative review): full sortable holdings table,
  sector/cap-tier/vol-tier breakdowns, analyst targets, 3m performance
  flags, MPT summary, and the efficient-frontier/correlation-heatmap plots —
  always current, refreshed by the pipeline 3x/day. This required extending
  `run_pipeline.py` (previously it only refreshed prices/cost-basis/alerts,
  never the enrichment columns — `Sector`/`Cap_Tier`/`Vol_Tier`/`Gain_3m`/etc
  were silently missing from every headless run's `combined.json` until
  this fix) to also call `enrich.symbol_metrics()`/`classify_sectors()` and
  the new `analytics.mpt_metrics()`/`efficient_frontier()`/
  `correlation_matrix()` → `plotting.py`'s Agg-backend plot savers. The
  notebook's cells 16/17 now call the same `plotting.py` functions (via a
  `build_*_figure()`/`save_*_plot()` split so the notebook keeps its
  interactive `plt.show()` while the headless pipeline just saves+closes).

### What's left — needs you, not more code

1. **Deploy to the VM**: `git push` → `python env_sync.py git_pull fidata` →
   `python env_sync.py install_reqs fidata` → `python env_sync.py push_env fidata`
   → `python env_sync.py deploy_panel` (for the new blueprints/templates) →
   copy `fiData/systemd/*.service` and `*.timer` to the VM's systemd unit dir,
   then `systemctl daemon-reload && systemctl enable --now
   fidata-pipeline.timer fidata-daily-review.timer fidata-weekly-review.timer`.
2. **`ANTHROPIC_API_KEY` / model check**: `ai_review.py` defaults to
   `claude-sonnet-4-6` (matching `todo_list/franklin/coach.py`'s existing
   convention) with an override via `FIDATA_COACH_MODEL` — confirm that's
   still the model you want before the first real weekly review runs.
3. Run the two verification steps that need live Claude credentials, which
   weren't exercised this session: one real `daily_summary()`/
   `weekly_deep_review()` call against the live `ANTHROPIC_API_KEY`.
4. `python env_sync.py check` currently flags `fidata`'s venv as
   missing/unreachable on the VM (expected — `install_reqs fidata` hasn't
   been run there yet as of this writing) and a few pre-existing unrelated
   drift items in other projects (`arcade`, `fridge-recipes`,
   `semantic_task_manager`) — worth a look next time you touch those.

### Update (2026-08-06): full notebook-data parity on /positions

You asked to see *everything* the notebook computes on the `/positions` webapp, not just a
subset. Audited every cell against what was actually exported/rendered and closed three
kinds of gap:

- **Template-only gaps** (data already existed, just wasn't shown): `flags.json`'s
  `high_trailing_pe`/`high_forward_pe`/`low_forward_pe`, `targets.json`'s `tightest`,
  `earnings.json`, `recommendations.json`, and `accounts.json` are now all rendered on
  `/positions`. The Holdings table gained Days_Held, Return %, Ann_Vol, Sharpe_1yr, Beta,
  and Cost_Basis_Source columns (all were already in `combined.json`, just not shown).
- **A real data gap, found while doing this**: `mpt_metrics()` returns per-symbol
  `beta_alpha` (Beta/Alpha_pct vs SPY) — the notebook's cell 15 always merged this into
  `combined`, but `run_pipeline.py` never did, so **every headless pipeline run silently
  dropped Beta/Alpha_pct from `combined.json`** — same class of bug as the Sector/Cap_Tier
  gap found earlier. Fixed by computing `mpt_metrics()` *before* `export_app_data()` in
  `run_pipeline.py` and merging `Beta`/`Alpha_pct` in, same pattern as Sector/Cap_Tier/Vol_Tier.
- **Never exported anywhere**: cell 2's closed-positions/realized-gains summary, cell 3's
  Capital Tracking & Cash-Adjusted Performance (annual activity, cost-basis-vs-market-value,
  overall ROIC), cell 15's risk-contribution table, and cell 16's max-Sharpe weight
  allocation were all print-only. Added `analytics.closed_positions_summary()` and
  `analytics.capital_performance()`; `run_pipeline.py` now writes all four into one new
  `data/portfolio_extras.json`. Notebook cells 2 and 3 now call these same functions instead
  of duplicating the arithmetic inline (same "cell becomes thin orchestration" pattern used
  throughout this refactor) — verified their printed output is unchanged.
- **Bonus fix while in this code**: `_sector_summary()` and the new `capital_performance()`
  helpers were both accidentally calling `.reset_index()` before passing to
  `_df_to_records()`, which *also* resets the index — this leaked a spurious `"index": 0`
  field into every `sectors.json` record (harmless, since nothing read it, but sloppy).
  Fixed by removing the redundant pre-reset.
- **New**: `panel/positions_routes.py` reads fiData's own `.env` (`ACC_<suffix>=<name>`) to
  label the new Per-Account Breakdown section with friendly names instead of raw account
  suffixes — same mapping the notebook's cell 0 already uses.

Verified: `pytest fiData/tests/` green, `run_pipeline.py` run locally end-to-end (confirmed
`Beta`/`Alpha_pct` populate in `combined.json`, `portfolio_extras.json` has sane numbers
matching the notebook's own cell 2/3 output), Flask test-client smoke tests on `/positions/`
with both real data (zero empty-state cards) and all data files missing (clean 200 with
every section's empty-state fallback, no crash).

### Update (2026-08-11/12): systemd timers actually installed, backup + integrity test,
### clean-slate cache rebuild, and a real env-loading bug found in the process

The `/fidata` Jobs card was correctly reporting `DISABLED`/"Timer not scheduled" for all
three jobs — not a UI bug, the systemd units had genuinely never been installed on the VM.
Fixed for real this time:

- **`fiData/systemd/*.service`** were using `%h/apps/fidata` with no `User=`, which doesn't
  reliably resolve for system-wide units. Rewrote all three to match the confirmed-working
  convention from `/etc/systemd/system/app-todo.service` (`User=ai1`, absolute
  `/home/ai1/apps/fidata/...` paths, `EnvironmentFile=.../fidata/.env`). Installed on the VM,
  `daemon-reload`, `enable --now` all three timers — confirmed via
  `systemctl list-timers` and `/fidata/api/status` (all three now `enabled: true` with a real
  `next_run`).
- **The VM had no venv for fiData at all** (`install_reqs` was never run there) — ran
  `env_sync.py install_reqs fidata` to fix.
- **Found via live testing, not by inspection**: `/fidata`'s "Run Now" button 500'd. Root
  cause — `systemctl start <oneshot>.service` blocks *synchronously* until the unit finishes
  (a multi-minute yfinance-heavy run), blowing past `panel/fidata_routes.py`'s `_run()`
  helper's 10s subprocess timeout, even though the job itself had started fine. Fixed with
  `systemctl --no-block start`, so the button returns immediately and `/api/status` is
  polled for progress. Verified end-to-end after the fix: pipeline run, daily-review run
  (real Claude-generated summary + Telegram send) — the whole systemd → pipeline → Claude →
  Telegram chain confirmed working live on the VM.
- **Backup**: zipped `accounts/`/`buysell/`/`past/` to
  `~/Documents/data_backups/fidata_broker_export_<date>.zip`. Ran a one-time integrity test
  — unzipped into a scratch dir, ran the exact same `load_positions()`/`load_transactions()`/
  `load_realized_lots()` functions against it, diffed against the live directories: exact
  match on all 10 accounts, `tx_df`, and `sold_df`. Scratch dir removed after.
- **Clean-slate cache rebuild**: several real bugs (Sector/Cap/Vol, then Beta/Alpha, both
  silently missing from every run until recently) meant existing derived caches might not
  reflect the current, fixed code. Moved (not deleted) `historical.csv`, `sectors.csv`,
  `earnings.csv`, `portfolio.csv`, `file_clean.csv`, the two plot PNGs, all of `app_data/`,
  and only the fiData-pipeline-generated files under `data/` (**not** the unrelated
  reference spreadsheets — `ai_semi_moat.xlsx`, `fredgraph.xlsx`, `Holdings.xlsx`, the "Tech
  Bubbles" files, etc. — that happen to also live in `data/` from before this project existed)
  into `fiData/_pre_reset_backup_<date>/`. Reran `run_pipeline.py` from scratch.
- **Second real bug, found because the clean-slate run made it visible**: `telegram_alert.py`
  reads `FI_BOT_ID`/`OWNER_CHAT_ID` from `os.environ` at **module import time**, but
  `run_pipeline.py` only loaded `.env` inside `if __name__ == '__main__':` — which runs
  *after* all top-level imports (including `from telegram_alert import send_telegram`). So
  every local/manual `python run_pipeline.py` run silently had Telegram disabled, the whole
  time, regardless of `.env` being correct — it only ever worked under systemd because
  `EnvironmentFile=` populates `os.environ` before the interpreter even starts. Fixed by
  moving the `.env` load to the very top of `run_pipeline.py`, before any local imports;
  `run_daily_review.py`/`run_weekly_review.py` get this transitively since they import
  `run_pipeline` first (before `ai_review`/`telegram_alert`) — verified with a fully clean
  `env -i` subprocess that `FI_BOT_ID`/`ANTHROPIC_API_KEY` are visible after import with no
  other env setup.
- Post-rebuild verification: `combined.json` has `Beta`/`Alpha_pct` populated for all 122
  equity symbols (0 missing), `sectors.json` has no stray `"index"` field, all four
  `portfolio_extras.json` keys present with sane data, `pytest fiData/tests/` green.

**Note**: the systemd/env-loading fixes above are committed locally but the VM's
`~/apps/fidata` clone predates them (git-pulled before this update) — the VM currently runs
the pre-fix `run_pipeline.py`. Since it's driven by `EnvironmentFile=` under systemd, Telegram
already works fine there regardless; the fix mainly matters for manual/local runs. Next
`git push` + `env_sync.py git_pull fidata` will bring the VM copy current.

### Update (2026-08-13): two live data-correctness bugs, a 3.4x faster run, and a
### local-only web viewer

Restored the project onto a new machine (see the plan file), then audited it. Two bugs
were corrupting numbers in daily use — both reproduced against real data, not inferred:

- **Fidelity account IDs parsed as floats.** `parsers/transactions.py` did
  `str(row['Account Number']).strip()[-4:]` on a column pandas types `float64`, so
  `236369828.0` became `'28.0'`, matching no position account. Two consequences, both
  active: `Accounts_History.csv` overlapped the per-account `History_for_Account_*.csv`
  exports and **45 of 393 trades were double-counted**, and `infer_missing_trades` could
  not cross-reference known trades so it re-invented ~$21.8K of BUYs already on file.
  Fixed with a single `parsers/common.account_key()` used by every parser (positions and
  transactions), plus a dedupe on `(Symbol, Date, Action, Quantity, Price)` in
  `load_transactions`. It drops leading zeros deliberately — that reproduces what
  `fidelity.py` always emitted, so `accounts.json` keys and the `ACC_*` map still work.
  Net effect: `Avg_Buy_Price` moved on **17 symbols** (SG +23.6%, VRT −18.5%, RDDT
  −13.1%, SOXL −12.6%); both loud ones now reconcile exactly to the raw broker rows.
  ROIC 13.43% → 14.27%. E*Trade's `'0898'` vs Fidelity's `'898'` — the same account —
  now agree too.
- **`historical.csv` had stopped appending.** `enrich.py` did
  `hist_df[sym] = hist_df[sym].combine_first(new_close)`; `DataFrame.__setitem__` aligns
  the RHS to the *existing* index, so every date not already present was dropped — and
  since `fetch_start = last_date + 1 day`, that was **100% of every incremental fetch**.
  The file was rewritten identically each run while `Gain_3m`/`Sharpe_*`/`Ann_Vol`/
  `Beta`/MPT silently froze at the last from-scratch rebuild. It only looked healthy
  because the 08-11 clean-slate rebuild sent every symbol down the working `else` branch.
  Fixed by reindexing first in both branches.
- **Realized-gain files in `accounts/`** are now read by `load_realized_lots` (it takes
  extra dirs), and `detect_format` gained a `'realized_gain'` branch so they stop
  printing "unrecognised format" three times a run. Recovered 2 TRGP lots worth
  **$1,061.90** of realized gain that no `buysell/` file contained.

**Tests: 14 → 74.** `tests/test_export_schema.py` pins the full 35-column `combined.json`
schema — including a check against the *live* pipeline output, which is what actually
catches the recurring "column computed then never merged" bug (Sector/Cap_Tier/Vol_Tier,
then Beta/Alpha_pct). `tests/test_cost_basis.py` covers account-key normalization, the
dedupe, and every `Cost_Basis_Source` branch. `tests/test_web_app.py` walks every viewer
route twice — real data, then empty directories.

**Runtime 3min → 53s, with zero 404s in the log.** It was ~400-500 sequential yfinance
round trips; the numeric core measures under 2s, so every win was in call count.
Removed the `upgrades_downgrades` fetch entirely (84 calls/run, result discarded by every
caller since nothing ever wrote `upgrades.csv`); replaced the stale hand-maintained
`DEFAULT_ETF_SKIP` with a `Quote_Type != 'EQUITY'` gate off the sector cache (11 held
ETFs were missing from the list — that was the source of the SOXL/SPMO/VT 404s);
batched `refresh_historical` into one `yf.download` per distinct start date instead of
122 `Ticker.history()` calls. Also stopped `classify_sectors` caching its own failures —
one transient 404 used to pin a symbol to `Sector=Unknown` permanently (QVMT was stuck
that way), and the placeholders are now evicted on load so they retry.

**Review jobs no longer re-run the pipeline.** `run_daily_review.py` and
`run_weekly_review.py` each called `run_pipeline.run()` in full, making the systemd
schedule 5 complete refreshes/day (6 on Sunday), each re-sending alerts and rewriting
`last_run_snapshot.csv`. Both now use `run_pipeline.load_last_run()`, which rebuilds the
same dict from disk in ~1.5s with no network (MPT metrics recomputed in-process from
`historical.csv`; it reproduces `mpt_summary.json` exactly). `run_pipeline` writes
`data/last_alerts.json` so the daily summary still knows what was sent.

**`local_server.py` — local-only web viewer.** Flask, run on demand, read-only over
whatever the last pipeline run left on disk; it never fetches, never triggers the
pipeline, and never sends Telegram, so opening it cannot perturb the scheduled jobs'
state. Nine pages (overview, holdings, accounts, sectors, flags, analysts, risk, capital,
chart) plus `/review` for the weekly Claude reports. The efficient-frontier and
correlation PNGs are served from a two-entry allowlist — the repo root holds `.env` and
every broker export, so it must never be a Flask static folder — while the per-symbol
price/drawdown chart renders on demand via the new
`plotting.build_price_drawdown_figure()`, which `viewer_app.py` now shares (its `fig=None`
parameter exists so the Tk canvas can pass in its persistent Figure). That port also
fixed the chart's hardcoded `rf = 0.043`, which disagreed with the live rate everywhere
else. `COL_FMTS` moved into `app_data_io.py` and gained the ~15 columns it was missing.

Gated on `FIDATA_LOCAL=1`, checked both in `create_app()` and in `__main__` (exit 2 with
a message, nothing bound). **Do not put `FIDATA_LOCAL` in `~/code20/.env.master`** —
`env_sync.py push_env` would propagate it to the VM and defeat the gate. Flask/rich/
prompt_toolkit live in a separate `requirements-local.txt` so `install_reqs` leaves the
VM venv alone; that also fixes `viewer.py`, which imported `rich`/`prompt_toolkit`
without ever declaring them and could not run in a fresh env.

**Known, not fixed — stale share counts after a split.** The broker exports are from
2026-06-18 and KORU did a 20:1 forward split on 2026-07-15. The pipeline keeps the export's
share count and multiplies by the live price, so KORU shows $473 instead of ~$9,458 —
the portfolio is understated by about **$8,985**. It's the only affected holding today.
Fresh broker exports fix it; a split guard (compare `yf.Ticker(s).splits` after the export
date against the held quantity) would stop it recurring silently.

### Update (2026-08-13b): stock splits

Splits hit this book about **four times a year** — 13 events across 12 symbols since 2023
(NVDA, AVGO, NFLX, WMT, IBKR, VUG, MEXX, NVO, HTHIY, MHVIY, OPPJ, and KORU twice, one of
those a 1:10 reverse). Brokers restate share counts, so anything already inside an export is
consistent; the exposure is the gap between downloading exports and now. With exports ~2
months old that is roughly **0.7 splits per refresh cycle**. There was no split handling
anywhere — `auto_adjust=True` was the only split-aware line, and it never reached cached rows.

One was live: **KORU 20:1 on 2026-07-15**, 27 days after the exports holding it, so a stale
22-share count was multiplied by the live post-split price — **$472.89 shown against a true
$9,438**, and a 0.069% portfolio weight instead of 1.350%.

**The "just re-download your exports" advice in the previous update was wrong** — it would
have made things worse, in two ways, both now demonstrated:

- **Cost basis would have exploded.** All 13 KORU buys are recorded pre-split. Today's error
  self-cancels (pre-split price x pre-split quantity ≈ correct dollars); fixing only the
  quantity gives `440 x $662.30 = $291,412`.
- **A phantom trade would have been fabricated.** A split is not a transaction
  (`_parse_action` returns None), so `infer_missing_trades` books the quantity jump as a BUY.
  Verified by simulating a post-split export against the real snapshots: the old path produced
  a spurious `BUY 76 KORU @ $55.05` on top of the genuine trade — **$8,596 of inferred capital
  against a true $4,413**. The new path produces exactly one correct row.

**Design — `splits.py`.** Every stale input carries a date, so each is normalized into today's
share terms as it enters the pipeline. `factor_since(table, sym, date)` is the cumulative ratio
after `date` (reverse splits compose naturally, being < 1), applied at five boundaries:
positions (stale export quantity), snapshots (before `infer_missing_trades` diffs them),
transactions (pre-split fills, with `Price x Quantity` preserved so `capital_deployed` dollars
don't move), `historical.csv` (drop and refetch a symbol's column rather than appending across
a split — `combine_first` never re-adjusts cached rows, so the old path would have grown a
-95% cliff at the next split), and `alerts.detect_big_moves` (an ex-date otherwise reads as
`📉 KORU: -95.0%`).

**Safety.** Auto-adjusting share counts is only safe if a bad adjustment fails loudly, so every
adjustment is gated on the export's own arithmetic: a broker export is internally consistent,
so `market_value / (quantity x split-adjusted close on the export date)` measures the ratio
directly. It must agree with the reported ratio, and a measured ~1.0 means the export was
already restated — that case is skipped with a message rather than silently 20x-ing a correct
position. Idempotence is verified: re-running gives 440 shares, never 8,800, and a
freshly-downloaded post-split export is correctly left alone.

**Export dating.** Filename date (7 accounts) → an in-file `as of` header (acct 898) → price
matching against `historical.csv` for E*Trade's `PortfolioDownload*.csv`, which carries no date
anywhere and whose mtime is meaningless after copying between machines (that recovers
2026-06-04 for accts 4919/1297). Unknown dates are treated as fresh: guessing too new is a
no-op, guessing too old double-applies a split.

**Feed.** `enrich._download_closes` now passes `actions=True`, so the split table rides the
price download at zero extra round trips, cached to `data/splits.csv`. One caveat found in
testing: the incremental refresh only fetches dates *after* the last cached row, so its window
can never contain a split that already happened — coverage is therefore seeded once back to
`CUTOFF` (`splits.needs_seed` / `enrich.seed_splits`) and maintained incrementally after.

**Also fixed while in the blast radius:** the realized-lot dedup key used `Quantity`, which a
broker restates after a split — switched to the split-invariant `Gain_Loss`, which additionally
recovered two genuinely distinct AVGO lots (cost $139.85 vs $139.44) the old key had merged,
worth **$215.70** of realized gain. `load_positions` now orders by export date rather than
mtime, so a re-downloaded older export can't silently override a newer one. `COL_FMTS`
formats quantities without truncating the fractional shares splits produce. The single-file
parser dispatch moved into `parsers.parse_file` so `load_positions` and `splits.export_dates`
share it.

**Result:** KORU 22 → 440 shares, `$472.89` → `$9,438.04`, `Avg_Buy_Price` $724.96 → $36.25
with cost basis unchanged at $15,949 (the dollars were always right), unrealized G/L
-$15,476 → -$6,511, portfolio weight 0.069% → 1.350%. The other 11 splits all predate their
exports and are correctly inert (`Split_Factor == 1.0`). Tests 74 → **123**.
