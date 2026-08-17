#!/usr/bin/env python3
"""viewer_app.py — GUI portfolio viewer for fiData"""
import tkinter as tk
from tkinter import ttk, messagebox
import os, sys
import pandas as pd
import numpy as np
from datetime import date, timedelta

try:
    import matplotlib
    import matplotlib.ticker as mticker
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    import matplotlib.dates as mdates
    _MPL = True
except ImportError:
    _MPL = False

DATA_DIR  = os.path.dirname(os.path.abspath(__file__))
APP_DATA  = os.path.join(DATA_DIR, 'app_data')
HIST_FILE = os.path.join(DATA_DIR, 'historical.csv')

from app_data_io import COL_FMTS, load_json, fmt

MPT_SUMMARY_FILE = os.path.join(DATA_DIR, 'data', 'mpt_summary.json')


def _rf_annual(default: float = 0.043) -> float:
    """The pipeline's live risk-free rate, falling back to the same constant
    enrich.risk_free_rate() uses. This chart used to hardcode 0.043 while the
    rest of the app ran on the real rate."""
    import json
    try:
        with open(MPT_SUMMARY_FILE) as f:
            return float(json.load(f).get('rf_annual', default))
    except Exception:
        return default

# ── Theme ──────────────────────────────────────────────────────────────────────
BG      = '#1e1e2e'
PANEL   = '#2a2a3e'
ACCENT  = '#7c6af7'
TEXT    = '#cdd6f4'
DIMTEXT = '#6c7086'
GREEN   = '#a6e3a1'
RED     = '#f38ba8'
YELLOW  = '#f9e2af'
BLUE    = '#89b4fa'

FT      = ('Segoe UI', 11)
FT_B    = ('Segoe UI', 11, 'bold')
FT_H    = ('Segoe UI', 13, 'bold')
FT_BIG  = ('Segoe UI', 20, 'bold')

# Column format map lives in app_data_io.COL_FMTS so this viewer and the
# local web viewer share one definition.


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('fiData Portfolio Viewer')
        self.configure(bg=BG)
        self.geometry('1300x820')
        self.minsize(900, 600)

        if not os.path.isdir(APP_DATA):
            messagebox.showerror('Missing data',
                'app_data/ not found.\nRun mystocks.ipynb first.')
            sys.exit(1)

        self._setup_styles()
        self._build_layout()
        self._show_splash()

    # ── Styles ─────────────────────────────────────────────────────────────────
    def _setup_styles(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('Dark.Treeview',
            background=PANEL, foreground=TEXT,
            fieldbackground=PANEL, rowheight=24, font=FT)
        s.configure('Dark.Treeview.Heading',
            background='#3a3a5e', foreground=TEXT,
            font=('Segoe UI', 10, 'bold'), relief='flat')
        s.map('Dark.Treeview',
            background=[('selected', ACCENT)],
            foreground=[('selected', 'white')])
        s.configure('TNotebook', background=BG, borderwidth=0)
        s.configure('TNotebook.Tab',
            background=PANEL, foreground=TEXT, padding=[12, 5])
        s.map('TNotebook.Tab',
            background=[('selected', ACCENT)],
            foreground=[('selected', 'white')])
        s.configure('TCombobox',
            fieldbackground='#3a3a4e', background='#3a3a4e',
            foreground=TEXT, selectbackground=ACCENT)
        s.map('TCombobox',
            fieldbackground=[('readonly', '#3a3a4e')],
            foreground=[('readonly', TEXT)])

    # ── Layout ─────────────────────────────────────────────────────────────────
    def _build_layout(self):
        topbar = tk.Frame(self, bg=PANEL, height=50)
        topbar.pack(fill='x')
        topbar.pack_propagate(False)

        tk.Label(topbar, text='fiData', font=FT_BIG,
                 bg=PANEL, fg=ACCENT).pack(side='left', padx=16)

        self._back = tk.Button(
            topbar, text='← Menu', font=FT,
            bg=PANEL, fg=PANEL,  # invisible on splash
            relief='flat', bd=0,
            activebackground=PANEL, activeforeground=TEXT,
            command=self._show_splash)
        self._back.pack(side='right', padx=16)

        self._content = tk.Frame(self, bg=BG)
        self._content.pack(fill='both', expand=True)

    def _clear(self):
        for w in self._content.winfo_children():
            w.destroy()

    # ── Splash ─────────────────────────────────────────────────────────────────
    def _show_splash(self):
        self._clear()
        self._back.config(fg=PANEL, state='disabled')

        outer = tk.Frame(self._content, bg=BG)
        outer.place(relx=0.5, rely=0.5, anchor='center')

        tk.Label(outer, text='Portfolio Viewer', font=FT_BIG,
                 bg=BG, fg=TEXT).pack(pady=(0, 6))
        tk.Label(outer, text='Select a view', font=FT,
                 bg=BG, fg=DIMTEXT).pack(pady=(0, 24))

        MENU = [
            ('Portfolio Overview',    self._view_combined),
            ('Individual Accounts',   self._view_accounts),
            ('Sectors',               self._view_sectors),
            ('Flags',                 self._view_flags),
            ('Analyst Targets',       self._view_targets),
            ('Earnings Calendar',     self._view_earnings),
            ('Recommendations',       self._view_recs),
            ('Upgrades / Downgrades', self._view_upgrades),
            ('Stock Chart',           self._view_chart),
        ]

        grid = tk.Frame(outer, bg=BG)
        grid.pack()
        for i, (lbl, cmd) in enumerate(MENU):
            r, c = divmod(i, 3)
            tk.Button(
                grid, text=lbl, font=FT_H, width=20, height=2,
                bg=PANEL, fg=TEXT, relief='flat',
                activebackground=ACCENT, activeforeground='white',
                command=cmd
            ).grid(row=r, column=c, padx=8, pady=6)

    def _enter_view(self):
        self._back.config(fg=TEXT, state='normal')

    # ── Reusable table builder ─────────────────────────────────────────────────
    def _table(self, parent, rows, title=''):
        if not rows:
            tk.Label(parent, text='No data available.',
                     bg=BG, fg=DIMTEXT, font=FT).pack(pady=20)
            return

        skip = {'index'}
        cols = [k for k in rows[0] if k not in skip]

        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill='both', expand=True, padx=10, pady=(4, 8))

        if title:
            tk.Label(wrap, text=title, font=FT_H,
                     bg=BG, fg=ACCENT).pack(anchor='w', pady=(4, 6))

        frm = tk.Frame(wrap, bg=BG)
        frm.pack(fill='both', expand=True)

        tv  = ttk.Treeview(frm, columns=cols, show='headings',
                           style='Dark.Treeview')
        vsb = ttk.Scrollbar(frm, orient='vertical',   command=tv.yview)
        hsb = ttk.Scrollbar(frm, orient='horizontal', command=tv.xview)
        tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side='right',  fill='y')
        hsb.pack(side='bottom', fill='x')
        tv.pack(fill='both', expand=True)

        for col in cols:
            tv.heading(col, text=col.replace('_', ' '), anchor='w')
            tv.column(col, width=130, minwidth=60, anchor='w')

        for i, rec in enumerate(rows):
            vals = tuple(fmt(rec.get(c), COL_FMTS.get(c, 'str')) for c in cols)
            tv.insert('', 'end', values=vals,
                      tags=('even' if i % 2 == 0 else 'odd',))

        tv.tag_configure('even', background=PANEL)
        tv.tag_configure('odd',  background='#252535')

    # ── Table views ────────────────────────────────────────────────────────────

    def _view_combined(self):
        self._clear(); self._enter_view()
        f = tk.Frame(self._content, bg=BG)
        f.pack(fill='both', expand=True)
        self._table(f, load_json('combined.json'), 'Portfolio Overview')

    def _view_accounts(self):
        self._clear(); self._enter_view()
        accts = load_json('accounts.json')
        nb = ttk.Notebook(self._content)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        for aid in sorted(accts):
            rows = accts[aid]
            if not rows:
                continue
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text=f'  {aid}  ')
            self._table(f, rows)

    def _view_sectors(self):
        self._clear(); self._enter_view()
        data = load_json('sectors.json')
        nb = ttk.Notebook(self._content)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        for key, lbl in [('by_gics', 'By GICS Sector'),
                          ('by_cap',  'By Market Cap'),
                          ('by_vol',  'By Volatility')]:
            rows = data.get(key, [])
            if not rows:
                continue
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text=f'  {lbl}  ')
            self._table(f, rows, lbl)

    def _view_flags(self):
        self._clear(); self._enter_view()
        data = load_json('flags.json')
        nb = ttk.Notebook(self._content)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        for key, lbl in [
            ('high_trailing_pe', 'High Trailing P/E'),
            ('high_forward_pe',  'High Forward P/E'),
            ('worst_3m',         'Worst 3-Month'),
            ('low_forward_pe',   'Low Forward P/E'),
            ('best_3m',          'Best 3-Month'),
        ]:
            rows = data.get(key, [])
            if not rows:
                continue
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text=f'  {lbl}  ')
            self._table(f, rows, lbl)

    def _view_targets(self):
        self._clear(); self._enter_view()
        data = load_json('targets.json')
        nb = ttk.Notebook(self._content)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        for key, lbl in [('overvalued',  'Target Below Price'),
                          ('most_upside', 'Most Upside'),
                          ('tightest',    'Tightest Consensus')]:
            rows = data.get(key, [])
            if not rows:
                continue
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text=f'  {lbl}  ')
            self._table(f, rows, lbl)

    def _view_earnings(self):
        self._clear(); self._enter_view()
        f = tk.Frame(self._content, bg=BG)
        f.pack(fill='both', expand=True)
        self._table(f, load_json('earnings.json'), 'Upcoming Earnings')

    def _view_recs(self):
        self._clear(); self._enter_view()
        f = tk.Frame(self._content, bg=BG)
        f.pack(fill='both', expand=True)
        self._table(f, load_json('recommendations.json'), 'Analyst Recommendations')

    def _view_upgrades(self):
        self._clear(); self._enter_view()
        try:
            data = load_json('upgrades.json')
        except FileNotFoundError:
            data = []
        f = tk.Frame(self._content, bg=BG)
        f.pack(fill='both', expand=True)
        self._table(f, data, 'Upgrades / Downgrades (last 90 days)')

    # ── Stock Chart ─────────────────────────────────────────────────────────────
    def _view_chart(self):
        if not _MPL:
            messagebox.showerror('Missing library',
                'matplotlib required: conda install matplotlib')
            return
        self._clear(); self._enter_view()

        # Load historical closes
        try:
            hist = pd.read_csv(HIST_FILE, index_col=0, parse_dates=True)
            hist.index = hist.index.tz_localize(None)
        except Exception as e:
            messagebox.showerror('Error', f'Cannot load historical.csv:\n{e}')
            return

        # Load positions for dollar-vol / qty context
        try:
            combined = load_json('combined.json')
            positions = {r['Symbol']: r for r in combined
                         if r.get('Symbol') not in (None, 'cash')}
        except Exception:
            positions = {}

        # Map display symbol → hist column (handles BRK/B → BRK-B)
        sym_map = {}
        for s in positions:
            ys = s.replace('/', '-')
            if ys in hist.columns:
                sym_map[s] = ys
            elif s in hist.columns:
                sym_map[s] = s
        if not sym_map:
            sym_map = {s: s for s in hist.columns}
        owned_syms = sorted(sym_map)

        max_d         = hist.index.max().date()
        default_start = str(max_d - timedelta(days=365))

        # ── Controls strip ────────────────────────────────────────────────────
        ctrl = tk.Frame(self._content, bg=PANEL, height=54)
        ctrl.pack(fill='x')
        ctrl.pack_propagate(False)

        def lbl(txt):
            return tk.Label(ctrl, text=txt, bg=PANEL, fg=TEXT, font=FT)

        lbl('Symbol:').pack(side='left', padx=(12, 4))
        sym_var = tk.StringVar(value=owned_syms[0] if owned_syms else '')
        sym_cb  = ttk.Combobox(ctrl, textvariable=sym_var, values=owned_syms,
                               width=10, font=FT, state='readonly')
        sym_cb.pack(side='left', padx=(0, 16))

        lbl('From:').pack(side='left', padx=(0, 4))
        start_var = tk.StringVar(value=default_start)
        tk.Entry(ctrl, textvariable=start_var, width=12, font=FT,
                 bg='#3a3a4e', fg=TEXT, insertbackground=TEXT,
                 relief='flat').pack(side='left', padx=(0, 16))

        lbl('To:').pack(side='left', padx=(0, 4))
        end_var = tk.StringVar(value=str(max_d))
        tk.Entry(ctrl, textvariable=end_var, width=12, font=FT,
                 bg='#3a3a4e', fg=TEXT, insertbackground=TEXT,
                 relief='flat').pack(side='left', padx=(0, 16))

        tk.Button(ctrl, text='Plot', font=FT_B,
                  bg=ACCENT, fg='white', relief='flat', padx=14,
                  activebackground='#9d8fff',
                  command=lambda: _plot()
                  ).pack(side='left')

        # ── Chart canvas ──────────────────────────────────────────────────────
        chart_f = tk.Frame(self._content, bg=BG)
        chart_f.pack(fill='both', expand=True)

        fig    = Figure(facecolor=BG)
        canvas = FigureCanvasTkAgg(fig, master=chart_f)
        canvas.get_tk_widget().pack(fill='both', expand=True)

        tb_f = tk.Frame(chart_f, bg='#2a2a2a')
        tb_f.pack(fill='x')
        NavigationToolbar2Tk(canvas, tb_f)

        # ── Plot function ──────────────────────────────────────────────────────
        def _plot():
            sym  = sym_var.get().strip()
            hcol = sym_map.get(sym, sym)
            if hcol not in hist.columns:
                messagebox.showwarning('Not found',
                    f'No historical data for {sym}')
                return
            try:
                t0 = pd.Timestamp(start_var.get().strip())
                t1 = pd.Timestamp(end_var.get().strip())
            except Exception:
                messagebox.showwarning('Bad date', 'Use YYYY-MM-DD format')
                return
            if t0 >= t1:
                messagebox.showwarning('Bad range', 'Start must be before end')
                return

            prices = hist[hcol].dropna()
            prices = prices[(prices.index >= t0) & (prices.index <= t1)]
            if len(prices) < 5:
                messagebox.showwarning('Insufficient data',
                    f'Only {len(prices)} points in selected range (need ≥ 5)')
                return

            # Drawing lives in plotting.build_price_drawdown_figure so the web
            # viewer renders the identical chart. `fig` is this canvas's own
            # persistent Figure — passed in to be cleared and redrawn in place.
            # Imported here, not at module scope, so this file still loads when
            # matplotlib is absent (the _MPL guard above already handles that).
            from plotting import build_price_drawdown_figure
            qty = float(positions.get(sym, {}).get('Quantity') or 0)
            build_price_drawdown_figure(prices, sym, qty=qty,
                                        rf_annual=_rf_annual(), fig=fig)
            canvas.draw()

        sym_cb.bind('<<ComboboxSelected>>', lambda _e: _plot())
        if owned_syms:
            _plot()


if __name__ == '__main__':
    App().mainloop()
