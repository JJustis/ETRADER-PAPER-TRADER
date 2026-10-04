#!/usr/bin/env python3
"""
E*TRADE REAL-TIME TRADING SANDBOX  (Pro Edition + News + Live Ticker)
=====================================================================

Performance-tuned build with a working data layer and clean shutdown:
- Live ticker moves every ~300 ms via a bounded random walk around the
  last real quote.
- Yahoo Finance is called through curl_cffi with Chrome TLS
  impersonation (plain requests gets HTTP 429 now).
- Automatic fallback to Stooq CSV if Yahoo fails.
- Chart redraws are throttled and only run for the visible tab.
- Dashboard refreshes at ~2 Hz instead of on every tick.
- Order/position/blotter rows are diffed, not rebuilt.
- Background fetch worker stops cleanly on window close (no
  "main thread is not in main loop" traceback).

Install:
    py -m pip install curl_cffi matplotlib requests

Run:
    py etraderprogv2.py
    py etraderprogv2.py --symbols AAPL,NVDA,AMD --cash 25000
    py etraderprogv2.py --reset
    py etraderprogv2.py --no-persist

Educational software. Not investment advice.
"""

import argparse
import csv
import io
import json
import queue
import random
import threading
import time
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# ---- HTTP layer: prefer curl_cffi, fall back to requests -------------
try:
    from curl_cffi import requests as http_requests
    _HAS_CURL_CFFI = True
except ImportError:
    import requests as http_requests
    _HAS_CURL_CFFI = False

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


# ---------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------

COLORS = {
    "bg":         "#0f1115",
    "bg_alt":     "#161923",
    "panel":      "#1b1f2a",
    "panel_alt":  "#222735",
    "border":     "#2a2f3d",
    "fg":         "#e6e9ef",
    "fg_dim":     "#8b93a7",
    "accent":     "#4f8cff",
    "accent_alt": "#7aa2ff",
    "up":         "#26c281",
    "down":       "#ff5c6c",
    "warn":       "#ffb454",
    "buy":        "#26c281",
    "sell":       "#ff5c6c",
    "grid":       "#232838",
    "flash":      "#1e3a2a",
    "flash_red":  "#3a1e22",
}

FONT        = "Segoe UI"
FONT_MONO   = "Consolas"
FONT_H1     = (FONT, 13, "bold")
FONT_H2     = (FONT, 10, "bold")
FONT_BODY   = (FONT, 10)
FONT_SMALL  = (FONT, 8)
FONT_MONO_M = (FONT_MONO, 10)
FONT_PRICE  = (FONT, 26, "bold")
FONT_CHG    = (FONT, 14, "bold")
FONT_METRIC = (FONT_MONO, 18, "bold")
FONT_LABEL  = (FONT, 9, "bold")
FONT_NEWS   = (FONT, 9)

STATE_DIR  = Path.home() / ".etrade_sandbox"
STATE_FILE = STATE_DIR / "state.json"

LIVE_TICK_MS    = 300
CHART_MIN_MS    = 600
DASHBOARD_HZ_MS = 500
QUOTE_POLL_MS   = 15000
REPLAY_STEP_MS  = 500


# ---------------------------------------------------------------------
# Order book (synthetic depth)
# ---------------------------------------------------------------------

class OrderBook:
    @staticmethod
    def snapshot(last_price, levels=5):
        if last_price <= 0:
            return [], []
        spread = max(round(last_price * 0.0001, 2), 0.01)
        half = spread / 2.0
        bid0 = round(last_price - half, 2)
        ask0 = round(last_price + half, 2)
        bids, asks = [], []
        for i in range(levels):
            level = spread * (i + 1)
            size = 100 + i * 75 + (i % 3) * 40
            bids.append((round(bid0 - level, 2), size))
            asks.append((round(ask0 + level, 2), size))
        return bids, asks


# ---------------------------------------------------------------------
# Market data (quotes + news) — with working TLS fingerprint
# ---------------------------------------------------------------------

class MarketData:
    CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    NEWS_URL  = (
        "https://feeds.finance.yahoo.com/rss/2.0/headline"
        "?s={symbol}&region=US&lang=en-US"
    )
    STOOQ_URL = "https://stooq.com/q/l/?s={symbol}.us&f=sd2t2ohlcv&h&e=csv"

    def __init__(self):
        if _HAS_CURL_CFFI:
            self.session = http_requests.Session(impersonate="chrome")
        else:
            import requests as _r
            self.session = _r.Session()
            self.session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
                "Accept": "application/json,text/html,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })

    # ----------------------------------------------------------------
    def quote(self, symbol, range_="1d", interval="1m", full=True):
        """
        full=True  -> 1m bars (first load / replay)
        full=False -> 5m bars, no history (anchor refresh)
        Falls back to Stooq if Yahoo rejects the request.
        """
        try:
            return self._yahoo_quote(symbol, range_, interval, full)
        except Exception as yahoo_exc:
            try:
                return self._stooq_quote(symbol)
            except Exception as stooq_exc:
                raise RuntimeError(
                    f"Yahoo failed ({yahoo_exc}); "
                    f"Stooq failed ({stooq_exc})")

    # ----------------------------------------------------------------
    def _yahoo_quote(self, symbol, range_, interval, full):
        params = {"range": range_, "interval": interval,
                  "includePrePost": "true"}
        r = self.session.get(
            self.CHART_URL.format(symbol=symbol.upper()),
            params=params, timeout=10,
        )
        if r.status_code == 429:
            raise RuntimeError("Yahoo rate-limited (HTTP 429)")
        r.raise_for_status()
        payload = r.json()
        result = payload.get("chart", {}).get("result")
        if not result:
            raise RuntimeError(f"No data for '{symbol}'.")

        data  = result[0]
        meta  = data.get("meta", {})
        quote = data["indicators"]["quote"][0]

        closes = [x for x in quote.get("close", []) if x is not None]
        if not closes:
            raise RuntimeError(f"No price data for '{symbol}'.")

        price = float(meta.get("regularMarketPrice") or closes[-1])
        previous = meta.get("previousClose")
        if previous:
            change = price - float(previous)
            pct    = change / float(previous) * 100
        else:
            change = pct = 0

        if not full:
            return {
                "symbol":    meta.get("symbol", symbol.upper()),
                "price":     price,
                "change":    change,
                "pct":       pct,
                "history":   None,
                "bars":      None,
                "timestamp": datetime.now(),
            }

        opens   = quote.get("open", [])
        highs   = quote.get("high", [])
        lows    = quote.get("low", [])
        volumes = quote.get("volume", [])
        stamps  = data.get("timestamp", [])

        bars = []
        n = min(len(closes), len(stamps))
        for i in range(n):
            bars.append({
                "t": stamps[i],
                "o": opens[i]   if i < len(opens)   else closes[i],
                "h": highs[i]   if i < len(highs)   else closes[i],
                "l": lows[i]    if i < len(lows)    else closes[i],
                "c": closes[i],
                "v": volumes[i] if i < len(volumes) else 0,
            })

        return {
            "symbol":    meta.get("symbol", symbol.upper()),
            "price":     price,
            "change":    change,
            "pct":       pct,
            "history":   closes[-120:],
            "bars":      bars[-500:],
            "timestamp": datetime.now(),
        }

    # ----------------------------------------------------------------
    def _stooq_quote(self, symbol):
        """Stooq CSV fallback: last close only, no intraday history."""
        url = self.STOOQ_URL.format(symbol=symbol.lower())
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        text = r.text.strip()
        if not text:
            raise RuntimeError("empty Stooq response")

        reader = csv.DictReader(io.StringIO(text))
        row = next(reader, None)
        if not row or row.get("Close") in (None, "", "N/D"):
            raise RuntimeError(f"no Stooq data for {symbol}")

        price = float(row["Close"])
        open_px = float(row["Open"]) \
            if row.get("Open") not in (None, "", "N/D") else price
        change = price - open_px
        pct = (change / open_px * 100) if open_px else 0.0

        return {
            "symbol":    symbol.upper(),
            "price":     price,
            "change":    change,
            "pct":       pct,
            "history":   [price],
            "bars":      [],
            "timestamp": datetime.now(),
        }

    # ----------------------------------------------------------------
    def news(self, symbol, max_items=15):
        url = self.NEWS_URL.format(symbol=symbol.upper())
        try:
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            raise RuntimeError(f"News feed error: {e}")

        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            source = src_el.text.strip() \
                if src_el is not None and src_el.text else "Yahoo Finance"
            if not title:
                continue
            items.append({
                "title":     title,
                "link":      link,
                "published": pub,
                "source":    source,
            })
            if len(items) >= max_items:
                break
        return items


# ---------------------------------------------------------------------
# Live price engine
# ---------------------------------------------------------------------

class LiveTicker:
    __slots__ = ("symbol", "anchor_price", "anchor_change", "price",
                 "prev_close", "band", "step", "paused", "drift",
                 "last_real_ts")

    def __init__(self, symbol, anchor_price, anchor_change=0.0):
        self.symbol = symbol
        self.anchor_price = float(anchor_price)
        self.anchor_change = float(anchor_change)
        self.price = float(anchor_price)
        self.prev_close = anchor_price - anchor_change
        self.band = max(self.anchor_price * 0.004, 0.01)
        self.step = max(self.band * 0.02, 0.001)
        self.paused = False
        self.last_real_ts = time.time()
        self.drift = random.uniform(-0.15, 0.15)

    def set_anchor(self, price, change=0.0):
        self.anchor_price = float(price)
        self.anchor_change = float(change)
        self.prev_close = price - change
        self.band = max(self.anchor_price * 0.004, 0.01)
        self.step = max(self.band * 0.02, 0.001)
        self.price = (self.price * 0.3) + (self.anchor_price * 0.7)
        self.last_real_ts = time.time()

    def tick(self):
        if self.paused:
            return self.price
        pull = (self.anchor_price - self.price) * 0.05
        shock = random.gauss(0, self.step)
        self.price += pull + shock + self.drift * self.step
        lo = self.anchor_price - self.band
        hi = self.anchor_price + self.band
        if self.price < lo:
            self.price = lo + abs(shock)
            self.drift = abs(self.drift)
        elif self.price > hi:
            self.price = hi - abs(shock)
            self.drift = -abs(self.drift)
        return self.price

    def change(self):
        return self.price - self.prev_close

    def pct(self):
        if self.prev_close:
            return self.change() / self.prev_close * 100
        return 0.0


# ---------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------

@dataclass
class Order:
    id: str
    symbol: str
    side: str
    shares: int
    limit_price: float
    created: str
    created_ts: float
    status: str = "OPEN"
    filled_price: float = 0.0
    filled_at: str = ""
    pnl: float = 0.0
    account: str = "default"
    parent_id: str = ""
    child_id: str = ""
    note: str = ""


@dataclass
class Position:
    shares: int = 0
    avg_price: float = 0.0


@dataclass
class Alert:
    id: str
    symbol: str
    op: str
    price: float
    fired: bool = False
    created: str = ""
    note: str = ""


@dataclass
class Account:
    name: str
    cash: float
    starting_cash: float
    positions: dict = field(default_factory=dict)
    orders: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    realized_pnl: float = 0.0
    total_bought: float = 0.0
    total_sold: float = 0.0
    total_fills: int = 0


# ---------------------------------------------------------------------
# Broker (multi-account)
# ---------------------------------------------------------------------

class Broker:
    def __init__(self, starting_cash=10000.0):
        self.accounts = {}
        self.current = "default"
        self.add_account("default", starting_cash)

    def add_account(self, name, cash):
        name = name.strip() or "default"
        if name in self.accounts:
            return self.accounts[name]
        self.accounts[name] = Account(
            name=name, cash=cash, starting_cash=cash)
        return self.accounts[name]

    def set_current(self, name):
        if name in self.accounts:
            self.current = name

    @property
    def acct(self) -> Account:
        return self.accounts[self.current]

    def position(self, symbol):
        p = self.acct.positions.get(symbol)
        if p is None:
            p = Position()
            self.acct.positions[symbol] = p
        return p

    def submit_limit(self, symbol, side, shares, limit_price,
                     parent_id="", note="", allow_short=False):
        if shares <= 0:
            raise ValueError("Shares must be greater than zero.")
        if limit_price <= 0:
            raise ValueError("Limit price must be greater than zero.")

        pos = self.position(symbol)

        if side == "BUY":
            estimated = shares * limit_price
            committed = sum(
                o.limit_price * o.shares
                for o in self.acct.orders
                if o.status == "OPEN" and o.side == "BUY"
            )
            if estimated + committed > self.acct.cash:
                raise ValueError(
                    f"Insufficient cash. Need ${estimated:,.2f} "
                    f"(plus ${committed:,.2f} reserved), "
                    f"have ${self.acct.cash:,.2f}."
                )
        if side == "SELL" and not allow_short:
            committed = sum(
                o.shares
                for o in self.acct.orders
                if o.status == "OPEN" and o.side == "SELL"
                and o.symbol == symbol
            )
            if shares + committed > pos.shares:
                raise ValueError(
                    f"Only {pos.shares} shares available to sell "
                    f"({committed} already reserved)."
                )

        order = Order(
            id=uuid.uuid4().hex[:8].upper(),
            symbol=symbol,
            side=side,
            shares=shares,
            limit_price=limit_price,
            created=datetime.now().strftime("%H:%M:%S"),
            created_ts=time.time(),
            account=self.current,
            parent_id=parent_id,
            note=note,
        )
        self.acct.orders.append(order)
        return order

    def submit_bracket(self, symbol, shares, buy_cap,
                       sell_target=None, sell_pct=None):
        parent = self.submit_limit(symbol, "BUY", shares, buy_cap,
                                   note="bracket parent")
        target = None
        if sell_target is not None:
            target = float(sell_target)
        elif sell_pct is not None:
            target = round(buy_cap * (1 + sell_pct / 100.0), 2)
        if target is None or target <= 0:
            raise ValueError("Bracket needs a valid sell target.")

        child = Order(
            id=uuid.uuid4().hex[:8].upper(),
            symbol=symbol, side="SELL", shares=shares,
            limit_price=target,
            created=datetime.now().strftime("%H:%M:%S"),
            created_ts=time.time(),
            status="PENDING_PARENT",
            account=self.current,
            parent_id=parent.id,
            note="bracket child",
        )
        parent.child_id = child.id
        self.acct.orders.append(child)
        return parent, child

    def cancel(self, order_id):
        for o in self.acct.orders:
            if o.id == order_id and o.status in ("OPEN", "PENDING_PARENT"):
                o.status = "CANCELLED"
                if o.parent_id:
                    for p in self.acct.orders:
                        if p.id == o.parent_id and p.status == "OPEN":
                            p.status = "CANCELLED"
                if o.child_id:
                    for c in self.acct.orders:
                        if c.id == o.child_id and \
                           c.status == "PENDING_PARENT":
                            c.status = "CANCELLED"
                return True
        return False

    def cancel_all(self):
        n = 0
        for o in self.acct.orders:
            if o.status in ("OPEN", "PENDING_PARENT"):
                o.status = "CANCELLED"
                n += 1
        return n

    def process(self, symbol, price):
        changed = False
        for order in self.acct.orders:
            if order.status != "OPEN" or order.symbol != symbol:
                continue
            if order.side == "BUY":
                if price > order.limit_price:
                    continue
            else:
                if price < order.limit_price:
                    continue

            fill_price = price
            pos = self.position(symbol)
            if order.side == "BUY":
                cost = fill_price * order.shares
                if cost > self.acct.cash:
                    order.status = "REJECTED"
                    changed = True
                    continue
                old_value = pos.shares * pos.avg_price
                new_shares = pos.shares + order.shares
                pos.avg_price = (old_value + cost) / new_shares
                pos.shares = new_shares
                self.acct.cash -= cost
                self.acct.total_bought += cost
                order.pnl = 0.0
                if order.child_id:
                    for c in self.acct.orders:
                        if c.id == order.child_id and \
                           c.status == "PENDING_PARENT":
                            c.status = "OPEN"
            else:
                proceeds = fill_price * order.shares
                pnl = (fill_price - pos.avg_price) * order.shares
                self.acct.cash += proceeds
                self.acct.realized_pnl += pnl
                pos.shares -= order.shares
                if pos.shares == 0:
                    pos.avg_price = 0
                self.acct.total_sold += proceeds
                order.pnl = pnl

            order.status = "FILLED"
            order.filled_price = fill_price
            order.filled_at = datetime.now().strftime("%H:%M:%S")
            self.acct.total_fills += 1
            changed = True
        return changed

    def panic_close(self, prices, cancel_first=True):
        if cancel_first:
            self.cancel_all()
        sold = 0
        for sym, pos in list(self.acct.positions.items()):
            if pos.shares <= 0:
                continue
            px = prices.get(sym)
            if px is None:
                continue
            proceeds = px * pos.shares
            pnl = (px - pos.avg_price) * pos.shares
            self.acct.cash += proceeds
            self.acct.realized_pnl += pnl
            self.acct.total_sold += proceeds
            order = Order(
                id=uuid.uuid4().hex[:8].upper(),
                symbol=sym, side="SELL", shares=pos.shares,
                limit_price=px,
                created=datetime.now().strftime("%H:%M:%S"),
                created_ts=time.time(),
                status="FILLED", filled_price=px,
                filled_at=datetime.now().strftime("%H:%M:%S"),
                pnl=pnl, account=self.current, note="PANIC CLOSE",
            )
            self.acct.orders.append(order)
            self.acct.total_fills += 1
            pos.shares = 0
            pos.avg_price = 0
            sold += 1
        return sold

    def market_value(self, prices):
        mv = 0.0
        for sym, pos in self.acct.positions.items():
            if pos.shares:
                px = prices.get(sym)
                if px is not None:
                    mv += pos.shares * px
        return mv

    def equity(self, prices):
        return self.acct.cash + self.market_value(prices)

    def unrealized(self, symbol, price):
        pos = self.position(symbol)
        return 0 if pos.shares == 0 \
            else (price - pos.avg_price) * pos.shares

    def total_unrealized(self, prices):
        total = 0.0
        for sym, pos in self.acct.positions.items():
            if pos.shares:
                px = prices.get(sym)
                if px is not None:
                    total += (px - pos.avg_price) * pos.shares
        return total

    def fills(self):
        return [o for o in self.acct.orders if o.status == "FILLED"]

    def open_orders(self):
        return [o for o in self.acct.orders
                if o.status in ("OPEN", "PENDING_PARENT")]

    def to_dict(self):
        return {
            "current": self.current,
            "accounts": {
                name: {
                    "name": a.name,
                    "cash": a.cash,
                    "starting_cash": a.starting_cash,
                    "realized_pnl": a.realized_pnl,
                    "total_bought": a.total_bought,
                    "total_sold": a.total_sold,
                    "total_fills": a.total_fills,
                    "positions": {s: asdict(p)
                                  for s, p in a.positions.items()},
                    "orders": [asdict(o) for o in a.orders],
                    "alerts": [asdict(al) for al in a.alerts],
                }
                for name, a in self.accounts.items()
            },
        }

    @classmethod
    def from_dict(cls, d):
        b = cls()
        b.accounts = {}
        b.current = d.get("current", "default")
        for name, ad in d.get("accounts", {}).items():
            a = Account(
                name=ad["name"], cash=ad["cash"],
                starting_cash=ad.get("starting_cash", ad["cash"]),
                realized_pnl=ad.get("realized_pnl", 0.0),
                total_bought=ad.get("total_bought", 0.0),
                total_sold=ad.get("total_sold", 0.0),
                total_fills=ad.get("total_fills", 0),
            )
            for s, pd_ in ad.get("positions", {}).items():
                a.positions[s] = Position(**pd_)
            for od in ad.get("orders", []):
                od.setdefault("created_ts", 0.0)
                od.setdefault("account", name)
                od.setdefault("parent_id", "")
                od.setdefault("child_id", "")
                od.setdefault("note", "")
                a.orders.append(Order(**od))
            for al in ad.get("alerts", []):
                a.alerts.append(Alert(**al))
            b.accounts[name] = a
        if b.current not in b.accounts:
            b.current = next(iter(b.accounts))
        return b


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def save_state(broker, path=STATE_FILE):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(broker.to_dict(), f, indent=2)
    except Exception as e:
        print(f"[save_state] {e}")


def load_state(path=STATE_FILE):
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return Broker.from_dict(json.load(f))
    except Exception as e:
        print(f"[load_state] {e}")
        return None


# ---------------------------------------------------------------------
# ScrollFrame
# ---------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    def __init__(self, master, bg=None, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                bg=bg or COLORS["panel"])
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda e: self._bind())
        self.canvas.bind("<Leave>", lambda e: self._unbind())

    def _on_inner(self, _):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _bind(self):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _unbind(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _wheel(self, event):
        if event.num == 4:
            d = -1
        elif event.num == 5:
            d = 1
        else:
            d = int(-event.delta / 120)
        self.canvas.yview_scroll(d, "units")


# ---------------------------------------------------------------------
# Metric card
# ---------------------------------------------------------------------

class MetricCard(tk.Frame):
    def __init__(self, master, label, value="--", color=None,
                 sublabel="", accent=None):
        super().__init__(master, bg=COLORS["panel_alt"],
                         highlightthickness=1,
                         highlightbackground=COLORS["border"])
        self.configure(padx=16, pady=12)

        tk.Label(self, text=label.upper(), bg=COLORS["panel_alt"],
                 fg=accent or COLORS["fg_dim"], font=FONT_LABEL
                 ).pack(anchor="w")

        self.value_lbl = tk.Label(self, text=value, bg=COLORS["panel_alt"],
                                  fg=color or COLORS["fg"], font=FONT_METRIC,
                                  anchor="w")
        self.value_lbl.pack(fill="x", pady=(6, 0))

        self.sub_lbl = tk.Label(self, text=sublabel, bg=COLORS["panel_alt"],
                                fg=COLORS["fg_dim"], font=FONT_SMALL,
                                anchor="w")
        self.sub_lbl.pack(fill="x", pady=(2, 0))

        self._last_value = None
        self._last_color = None
        self._last_sub = None

    def set(self, value, color=None, sublabel=None):
        if value != self._last_value:
            self.value_lbl.config(text=value)
            self._last_value = value
        if color and color != self._last_color:
            self.value_lbl.config(fg=color)
            self._last_color = color
        if sublabel is not None and sublabel != self._last_sub:
            self.sub_lbl.config(text=sublabel)
            self._last_sub = sublabel


# ---------------------------------------------------------------------
# Symbol tab
# ---------------------------------------------------------------------

class SymbolTab:
    def __init__(self, app, notebook, symbol):
        self.app = app
        self.symbol = symbol.upper()
        self.prices = deque(maxlen=120)
        self.last_quote = None
        self.replay_mode = False
        self.replay_bars = []
        self.replay_idx = 0
        self.replay_job = None
        self._news_widgets = []
        self._news_tick = 0

        self.ticker = None

        self._last_chart_draw = 0.0
        self._last_price_text = None
        self._last_change_text = None
        self._last_change_color = None
        self._last_book_tuple = None
        self._last_order_row_state = {}

        self.frame = ttk.Frame(notebook, style="TFrame")
        notebook.add(self.frame, text=f"  {self.symbol}  ")
        self._build()

    # ----------------------------------------------------------------
    def _build(self):
        outer_scroll = ScrollFrame(self.frame, bg=COLORS["bg"])
        outer_scroll.pack(fill="both", expand=True)
        content = outer_scroll.inner
        content.configure(style="TFrame")

        header = tk.Frame(content, bg=COLORS["bg_alt"])
        header.pack(fill="x", padx=10, pady=(10, 0))

        tk.Label(header, text=self.symbol, bg=COLORS["bg_alt"],
                 fg=COLORS["fg_dim"], font=(FONT, 11, "bold")
                 ).pack(side="left", padx=(12, 6), pady=10)

        self.price_label = tk.Label(header, text="--",
                                    bg=COLORS["bg_alt"], fg=COLORS["fg"],
                                    font=FONT_PRICE)
        self.price_label.pack(side="left", padx=(0, 12), pady=8)

        self.change_label = tk.Label(header, text="--",
                                     bg=COLORS["bg_alt"],
                                     fg=COLORS["fg_dim"], font=FONT_CHG)
        self.change_label.pack(side="left", pady=8)

        self.book_label = tk.Label(header, text="BID --  ASK --",
                                   bg=COLORS["bg_alt"],
                                   fg=COLORS["fg_dim"], font=FONT_MONO_M)
        self.book_label.pack(side="left", padx=14, pady=8)

        self.status_lbl = tk.Label(header, text="● CONNECTING…",
                                   bg=COLORS["bg_alt"], fg=COLORS["warn"],
                                   font=(FONT, 9, "bold"))
        self.status_lbl.pack(side="right", padx=(0, 12), pady=8)

        self.time_label = tk.Label(header, text="--",
                                   bg=COLORS["bg_alt"],
                                   fg=COLORS["fg_dim"], font=FONT_MONO_M)
        self.time_label.pack(side="right", padx=(0, 12), pady=8)

        replay = tk.Frame(content, bg=COLORS["bg"])
        replay.pack(fill="x", padx=10, pady=(6, 0))
        self.replay_btn = ttk.Button(replay, text="▶ START REPLAY",
                                     command=self._toggle_replay)
        self.replay_btn.pack(side="left")
        self.replay_label = tk.Label(replay, text="live mode",
                                     bg=COLORS["bg"], fg=COLORS["fg_dim"],
                                     font=FONT_SMALL)
        self.replay_label.pack(side="left", padx=10)

        top_split = tk.Frame(content, bg=COLORS["bg"], height=520)
        top_split.pack(fill="x", padx=10, pady=(8, 0))
        top_split.pack_propagate(False)

        horizontal = tk.PanedWindow(top_split, orient="horizontal",
                                    bg=COLORS["bg"], sashwidth=6,
                                    sashrelief="flat", borderwidth=0, bd=0)
        horizontal.pack(fill="both", expand=True)

        chart_panel = ttk.Frame(horizontal, style="Panel.TFrame")
        self.figure = Figure(figsize=(8, 5), dpi=100,
                             facecolor=COLORS["panel"])
        self.ax = self.figure.add_subplot(111)
        self._style_axes()
        self.figure.subplots_adjust(left=0.08, right=0.98,
                                    top=0.92, bottom=0.12)
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_panel)
        self.canvas.get_tk_widget().configure(bg=COLORS["panel"],
                                              highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True,
                                         padx=8, pady=8)
        horizontal.add(chart_panel, minsize=420, stretch="always")

        right_outer = ttk.Frame(horizontal, style="Panel.TFrame")
        horizontal.add(right_outer, minsize=360, width=380,
                       stretch="never")
        right_scroll = ScrollFrame(right_outer, bg=COLORS["panel"])
        right_scroll.pack(fill="both", expand=True)
        right = right_scroll.inner
        right.configure(style="Panel.TFrame")

        self._build_book_panel(right)
        self._build_trade_controls(right)
        self._build_account_panel(right)
        self._build_alerts_panel(right)
        self._build_safety_panel(right)

        self.frame.update_idletasks()
        try:
            horizontal.sash_place(0, 880, 0)
        except tk.TclError:
            pass

        orders_block = tk.Frame(content, bg=COLORS["bg"], height=260)
        orders_block.pack(fill="x", padx=10, pady=(8, 0))
        orders_block.pack_propagate(False)

        orders_panel = ttk.Labelframe(
            orders_block,
            text=f" Orders — {self.symbol}  ·  right-click to cancel ",
            style="TLabelframe")
        orders_panel.pack(fill="both", expand=True)
        self._build_orders_table(orders_panel)

        news_block = tk.Frame(content, bg=COLORS["bg"], height=260)
        news_block.pack(fill="x", padx=10, pady=(8, 12))
        news_block.pack_propagate(False)

        news_panel = ttk.Labelframe(
            news_block, text=f" Recent News — {self.symbol} ",
            style="TLabelframe")
        news_panel.pack(fill="both", expand=True)

        news_scroll = ScrollFrame(news_panel, bg=COLORS["panel"])
        news_scroll.pack(fill="both", expand=True, padx=8, pady=8)
        self.news_box = news_scroll.inner
        self.news_box.configure(style="Panel.TFrame")

        self.news_header = ttk.Label(
            self.news_box, text="Loading headlines…",
            style="Dim.TLabel", font=FONT_SMALL)
        self.news_header.pack(anchor="w", padx=4, pady=(2, 6))

        self.frame.after(200, self.refresh_news)

    # ----------------------------------------------------------------
    def _build_book_panel(self, parent):
        wrap = ttk.Frame(parent, style="Panel.TFrame",
                         padding=(16, 14, 16, 6))
        wrap.pack(fill="x")
        ttk.Label(wrap, text=f"Order Book · {self.symbol}",
                  style="H1.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Synthetic depth around the last trade.",
                  style="Dim.TLabel", font=FONT_SMALL,
                  wraplength=320, justify="left"
                  ).pack(anchor="w", pady=(2, 10))

        book = ttk.Frame(wrap, style="Panel.TFrame")
        book.pack(fill="x")
        book.columnconfigure(0, weight=1)
        book.columnconfigure(1, weight=1)

        ttk.Label(book, text="BID", style="Dim.TLabel",
                  font=FONT_LABEL, foreground=COLORS["buy"]
                  ).grid(row=0, column=0, sticky="w")
        ttk.Label(book, text="ASK", style="Dim.TLabel",
                  font=FONT_LABEL, foreground=COLORS["sell"]
                  ).grid(row=0, column=1, sticky="e")

        self.book_rows = []
        for i in range(5):
            bp = tk.Label(book, text="--", bg=COLORS["panel"],
                          fg=COLORS["buy"], font=FONT_MONO_M, anchor="w")
            bp.grid(row=i + 1, column=0, sticky="w", pady=1)
            bs = tk.Label(book, text="", bg=COLORS["panel"],
                          fg=COLORS["fg_dim"], font=FONT_SMALL, anchor="w")
            bs.grid(row=i + 1, column=0, sticky="w", padx=(70, 0), pady=1)

            ap = tk.Label(book, text="--", bg=COLORS["panel"],
                          fg=COLORS["sell"], font=FONT_MONO_M, anchor="e")
            ap.grid(row=i + 1, column=1, sticky="e", pady=1)
            as_ = tk.Label(book, text="", bg=COLORS["panel"],
                           fg=COLORS["fg_dim"], font=FONT_SMALL, anchor="e")
            as_.grid(row=i + 1, column=1, sticky="e",
                     padx=(0, 70), pady=1)

            self.book_rows.append({
                "bid_price": bp, "bid_size": bs,
                "ask_price": ap, "ask_size": as_})

        self.mid_label = tk.Label(wrap, text="MID --",
                                  bg=COLORS["panel"], fg=COLORS["fg_dim"],
                                  font=FONT_SMALL)
        self.mid_label.pack(fill="x", pady=(8, 0))

    # ----------------------------------------------------------------
    def _build_trade_controls(self, parent):
        wrap = ttk.Frame(parent, style="Panel.TFrame",
                         padding=(16, 14, 16, 6))
        wrap.pack(fill="x")
        ttk.Separator(wrap).pack(fill="x", pady=(0, 12))
        ttk.Label(wrap, text="Limit Trading", style="H1.TLabel"
                  ).pack(anchor="w")
        ttk.Label(wrap, text="Orders fill when price crosses your cap.",
                  style="Dim.TLabel", font=FONT_SMALL,
                  wraplength=320, justify="left"
                  ).pack(anchor="w", pady=(2, 12))

        quick = ttk.Frame(wrap, style="Panel.TFrame")
        quick.pack(fill="x", pady=(0, 10))
        quick.columnconfigure(0, weight=1)
        quick.columnconfigure(1, weight=1)
        ttk.Button(quick, text="USE BID",
                   command=self._fill_buy_with_bid
                   ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(quick, text="USE ASK",
                   command=self._fill_sell_with_ask
                   ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Label(wrap, text="Shares", style="H2.TLabel").pack(anchor="w")
        self.shares = tk.StringVar(value="1")
        ttk.Entry(wrap, textvariable=self.shares
                  ).pack(fill="x", pady=(4, 6))

        size_row = ttk.Frame(wrap, style="Panel.TFrame")
        size_row.pack(fill="x", pady=(0, 12))
        size_row.columnconfigure(0, weight=1)
        size_row.columnconfigure(1, weight=1)
        ttk.Label(size_row, text="or $ amount",
                  style="Dim.TLabel", font=FONT_SMALL
                  ).grid(row=0, column=0, sticky="w")
        self.dollar_amt = tk.StringVar()
        ttk.Entry(size_row, textvariable=self.dollar_amt, width=10
                  ).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(size_row, text="SIZE", command=self._size_from_dollars
                   ).grid(row=1, column=1, sticky="ew")

        ttk.Label(wrap, text="BUY price cap", style="H2.TLabel"
                  ).pack(anchor="w")
        self.buy_cap = tk.StringVar()
        ttk.Entry(wrap, textvariable=self.buy_cap
                  ).pack(fill="x", pady=(4, 2))
        ttk.Label(wrap, text="Maximum price you will pay.",
                  style="Dim.TLabel", font=FONT_SMALL
                  ).pack(anchor="w", pady=(0, 8))
        ttk.Button(wrap, text="PLACE BUY LIMIT  ▲", style="Buy.TButton",
                   command=lambda: self.place("BUY")
                   ).pack(fill="x", pady=(0, 6))

        ttk.Separator(wrap).pack(fill="x", pady=10)

        ttk.Label(wrap, text="Bracket (buy + auto-sell)",
                  style="H2.TLabel").pack(anchor="w")
        br = ttk.Frame(wrap, style="Panel.TFrame")
        br.pack(fill="x", pady=(4, 8))
        br.columnconfigure(0, weight=1)
        br.columnconfigure(1, weight=1)
        ttk.Label(br, text="sell target $", style="Dim.TLabel",
                  font=FONT_SMALL).grid(row=0, column=0, sticky="w")
        ttk.Label(br, text="sell target %", style="Dim.TLabel",
                  font=FONT_SMALL).grid(row=0, column=1, sticky="w")
        self.br_target = tk.StringVar()
        self.br_pct = tk.StringVar()
        ttk.Entry(br, textvariable=self.br_target, width=10
                  ).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        ttk.Entry(br, textvariable=self.br_pct, width=10
                  ).grid(row=1, column=1, sticky="ew")
        ttk.Button(wrap, text="PLACE BRACKET  ⚡",
                   command=self.place_bracket
                   ).pack(fill="x", pady=(0, 12))

        ttk.Separator(wrap).pack(fill="x", pady=6)

        ttk.Label(wrap, text="SELL price cap", style="H2.TLabel"
                  ).pack(anchor="w", pady=(6, 0))
        self.sell_cap = tk.StringVar()
        ttk.Entry(wrap, textvariable=self.sell_cap
                  ).pack(fill="x", pady=(4, 2))
        ttk.Label(wrap, text="Minimum price you will accept.",
                  style="Dim.TLabel", font=FONT_SMALL
                  ).pack(anchor="w", pady=(0, 8))
        ttk.Button(wrap, text="SELL LIMIT  ▼", style="Sell.TButton",
                   command=lambda: self.place("SELL")
                   ).pack(fill="x", pady=(0, 6))

    # ----------------------------------------------------------------
    def _build_account_panel(self, parent):
        wrap = ttk.Frame(parent, style="Panel.TFrame",
                         padding=(16, 10, 16, 10))
        wrap.pack(fill="x")
        ttk.Separator(wrap).pack(fill="x", pady=(0, 12))
        ttk.Label(wrap, text="This Symbol", style="H1.TLabel"
                  ).pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(wrap, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        self.acct_values = {}
        self._acct_last = {}
        rows = [("Position", "pos"), ("Avg price", "avg"),
                ("Market value", "mv"), ("Unrealized P/L", "unrealized")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label, style="Dim.TLabel",
                      font=FONT_SMALL).grid(row=i, column=0,
                                            sticky="w", pady=3)
            v = ttk.Label(grid, text="--", style="Panel.TLabel",
                          font=FONT_MONO_M, anchor="e")
            v.grid(row=i, column=1, sticky="e", pady=3)
            self.acct_values[key] = v

    # ----------------------------------------------------------------
    def _build_alerts_panel(self, parent):
        wrap = ttk.Frame(parent, style="Panel.TFrame",
                         padding=(16, 10, 16, 10))
        wrap.pack(fill="x")
        ttk.Separator(wrap).pack(fill="x", pady=(0, 12))
        ttk.Label(wrap, text="Price Alerts", style="H1.TLabel"
                  ).pack(anchor="w", pady=(0, 8))

        row = ttk.Frame(wrap, style="Panel.TFrame")
        row.pack(fill="x", pady=(0, 6))
        row.columnconfigure(0, weight=0)
        row.columnconfigure(1, weight=1)

        self.alert_op = tk.StringVar(value=">")
        ttk.Combobox(row, textvariable=self.alert_op, width=3,
                     values=[">", "<"], state="readonly"
                     ).grid(row=0, column=0, padx=(0, 4))
        self.alert_price = tk.StringVar()
        ttk.Entry(row, textvariable=self.alert_price
                  ).grid(row=0, column=1, sticky="ew")

        ttk.Button(wrap, text="ADD ALERT",
                   command=self._add_alert).pack(fill="x", pady=(0, 6))

        self.alerts_box = tk.Listbox(
            wrap, height=4, bg=COLORS["panel_alt"], fg=COLORS["fg"],
            selectbackground=COLORS["accent"], borderwidth=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
            font=FONT_SMALL)
        self.alerts_box.pack(fill="x")

        ttk.Button(wrap, text="CLEAR FIRED",
                   command=self._clear_fired_alerts
                   ).pack(fill="x", pady=(6, 0))

    def _build_safety_panel(self, parent):
        wrap = ttk.Frame(parent, style="Panel.TFrame",
                         padding=(16, 6, 16, 20))
        wrap.pack(fill="both", expand=True)
        ttk.Separator(wrap).pack(fill="x", pady=(0, 12))
        ttk.Label(wrap, text="Safety", style="H1.TLabel"
                  ).pack(anchor="w", pady=(0, 6))
        for text, color in [
            ("● LOCAL PAPER EXECUTION", COLORS["up"]),
            ("● NO LIVE E*TRADE ORDERS", COLORS["down"]),
            ("● LIMIT ORDERS ONLY", COLORS["warn"]),
        ]:
            tk.Label(wrap, text=text, bg=COLORS["panel"], fg=color,
                     font=(FONT, 9, "bold"), anchor="w"
                     ).pack(anchor="w", pady=2)

    def _build_orders_table(self, parent):
        container = ttk.Frame(parent, style="Panel.TFrame")
        container.pack(fill="both", expand=True, padx=8, pady=8)

        columns = ("id", "side", "shares", "limit", "live", "gap",
                   "status", "fill", "pnl", "time", "note")
        headings = {"id": "ID", "side": "SIDE", "shares": "SHARES",
                    "limit": "CAP", "live": "LIVE", "gap": "GAP TO CAP",
                    "status": "STATUS", "fill": "FILL", "pnl": "P/L",
                    "time": "TIME", "note": "NOTE"}

        self.orders = ttk.Treeview(container, columns=columns,
                                   show="headings", height=7)
        for col in columns:
            self.orders.heading(col, text=headings[col])
            self.orders.column(col, width=95, anchor="center")
        self.orders.column("note", width=140)

        vsb = ttk.Scrollbar(container, orient="vertical",
                            command=self.orders.yview)
        self.orders.configure(yscrollcommand=vsb.set)
        self.orders.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.orders.tag_configure("BUY",       foreground=COLORS["buy"])
        self.orders.tag_configure("SELL",      foreground=COLORS["sell"])
        self.orders.tag_configure("FILLED",    background=COLORS["panel_alt"])
        self.orders.tag_configure("REJECTED",  foreground=COLORS["down"])
        self.orders.tag_configure("CANCELLED", foreground=COLORS["fg_dim"])
        self.orders.tag_configure("INRANGE",   background=COLORS["flash"])
        self.orders.tag_configure("PENDING_PARENT",
                                  foreground=COLORS["warn"])

        self.orders.bind("<Button-3>", self._context_cancel)
        self.orders.bind("<Button-2>", self._context_cancel)

    def _context_cancel(self, event):
        row = self.orders.identify_row(event.y)
        if not row:
            return
        vals = self.orders.item(row, "values")
        if not vals:
            return
        oid = vals[0]
        for o in self.app.broker.acct.orders:
            if o.id == oid and o.status in ("OPEN", "PENDING_PARENT"):
                if messagebox.askyesno(
                    "Cancel order",
                    f"Cancel {oid}?\n{o.side} {o.shares} {o.symbol} "
                    f"@ ${o.limit_price:.2f}",
                ):
                    self.app.broker.cancel(oid)
                    self.app.refresh_all_views(force=True)
                    self.app.persist()
                return

    def _style_axes(self):
        self.ax.set_facecolor(COLORS["panel"])
        self.figure.patch.set_facecolor(COLORS["panel"])
        for spine in self.ax.spines.values():
            spine.set_color(COLORS["border"])
        self.ax.tick_params(colors=COLORS["fg_dim"], labelsize=8)
        self.ax.xaxis.label.set_color(COLORS["fg_dim"])
        self.ax.yaxis.label.set_color(COLORS["fg_dim"])
        self.ax.title.set_color(COLORS["fg"])
        self.ax.grid(True, alpha=0.15, color=COLORS["grid"])

    # ----------------------------------------------------------------
    def _fill_buy_with_bid(self):
        if not self.last_quote:
            return
        bids, _ = OrderBook.snapshot(self.last_quote["price"])
        if bids:
            self.buy_cap.set(f"{bids[0][0]:.2f}")

    def _fill_sell_with_ask(self):
        if not self.last_quote:
            return
        _, asks = OrderBook.snapshot(self.last_quote["price"])
        if asks:
            self.sell_cap.set(f"{asks[0][0]:.2f}")

    def _size_from_dollars(self):
        try:
            amt = float(self.dollar_amt.get())
        except ValueError:
            return
        ref = None
        if self.buy_cap.get():
            try:
                ref = float(self.buy_cap.get())
            except ValueError:
                ref = None
        if ref is None and self.last_quote:
            ref = self.last_quote["price"]
        if not ref:
            return
        shares = max(1, int(amt / ref))
        self.shares.set(str(shares))

    # ----------------------------------------------------------------
    def place(self, side):
        try:
            shares = int(self.shares.get())
            cap = float(self.buy_cap.get() if side == "BUY"
                        else self.sell_cap.get())
            if not self.last_quote:
                raise ValueError("No market price available yet.")
            current = self.last_quote["price"]

            order = self.app.broker.submit_limit(
                self.symbol, side, shares, cap)
            self.refresh_orders(force=True)
            self.app.refresh_all_views(force=True)
            self.app.persist()

            messagebox.showinfo(
                f"{side} LIMIT — {self.symbol}",
                f"{side} LIMIT\n\n"
                f"{shares} shares of {self.symbol}\n"
                f"Price cap: ${cap:.2f}\n"
                f"Current:  ${current:.2f}\n\n"
                f"Order ID: {order.id}",
            )
            self.app.broker.process(self.symbol, current)
            self.refresh_orders(force=True)
            self.app.refresh_all_views(force=True)
            self.app.persist()
        except Exception as exc:
            messagebox.showerror("Order rejected", str(exc))

    def place_bracket(self):
        try:
            shares = int(self.shares.get())
            buy_cap = float(self.buy_cap.get())
            target = None
            pct = None
            if self.br_target.get():
                target = float(self.br_target.get())
            if self.br_pct.get():
                pct = float(self.br_pct.get())
            if target is None and pct is None:
                raise ValueError("Give a bracket target ($ or %).")

            parent, child = self.app.broker.submit_bracket(
                self.symbol, shares, buy_cap,
                sell_target=target, sell_pct=pct)
            self.refresh_orders(force=True)
            self.app.refresh_all_views(force=True)
            self.app.persist()

            messagebox.showinfo(
                f"BRACKET — {self.symbol}",
                f"BUY {shares} @ ${parent.limit_price:.2f}\n"
                f"→ on fill, SELL {shares} @ ${child.limit_price:.2f}\n\n"
                f"Parent: {parent.id}   Child: {child.id}",
            )
            if self.last_quote:
                self.app.broker.process(self.symbol,
                                        self.last_quote["price"])
            self.refresh_orders(force=True)
            self.app.refresh_all_views(force=True)
            self.app.persist()
        except Exception as exc:
            messagebox.showerror("Bracket rejected", str(exc))

    # ----------------------------------------------------------------
    def _add_alert(self):
        try:
            px = float(self.alert_price.get())
            if px <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Alert", "Enter a positive price.")
            return
        a = Alert(id=uuid.uuid4().hex[:6].upper(),
                  symbol=self.symbol, op=self.alert_op.get(),
                  price=px,
                  created=datetime.now().strftime("%H:%M:%S"))
        self.app.broker.acct.alerts.append(a)
        self.alert_price.set("")
        self._refresh_alerts()
        self.app.persist()

    def _clear_fired_alerts(self):
        self.app.broker.acct.alerts = [
            a for a in self.app.broker.acct.alerts if not a.fired]
        self._refresh_alerts()
        self.app.persist()

    def _refresh_alerts(self):
        self.alerts_box.delete(0, tk.END)
        for a in self.app.broker.acct.alerts:
            if a.symbol != self.symbol:
                continue
            tag = "✓" if a.fired else "•"
            self.alerts_box.insert(
                tk.END,
                f"{tag} {a.symbol} {a.op} ${a.price:.2f}  ({a.created})")

    # ----------------------------------------------------------------
    def _toggle_replay(self):
        if self.replay_mode:
            self._stop_replay()
        else:
            self._start_replay()

    def _start_replay(self):
        if not self.last_quote or not self.last_quote.get("bars"):
            messagebox.showinfo(
                "Replay", "No historical bars loaded yet — wait for a quote.")
            return
        self.replay_bars = list(self.last_quote["bars"])
        self.replay_idx = 0
        self.replay_mode = True
        if self.ticker:
            self.ticker.paused = True
        self.prices.clear()
        self.replay_btn.config(text="■ STOP REPLAY")
        self.replay_label.config(
            text=f"replay {len(self.replay_bars)} bars @ 2x")
        self.status_lbl.config(text="● REPLAY", fg=COLORS["warn"])
        self._step_replay()

    def _stop_replay(self):
        self.replay_mode = False
        if self.replay_job:
            try:
                self.frame.after_cancel(self.replay_job)
            except Exception:
                pass
        self.replay_job = None
        if self.ticker:
            self.ticker.paused = False
        self.replay_btn.config(text="▶ START REPLAY")
        self.replay_label.config(text="live mode")
        self.status_lbl.config(text="● LIVE", fg=COLORS["up"])
        if self.last_quote:
            self.prices.clear()
            if self.last_quote.get("history"):
                self.prices.extend(self.last_quote["history"])
            self._last_chart_draw = 0.0
            self.draw_chart(force=True)

    def _step_replay(self):
        if not self.replay_mode or self.replay_idx >= len(self.replay_bars):
            self._stop_replay()
            return
        bar = self.replay_bars[self.replay_idx]
        self.replay_idx += 1
        px = bar["c"]
        self.prices.append(px)
        self.price_label.config(text=f"${px:,.2f}")
        self.time_label.config(
            text=datetime.fromtimestamp(bar["t"])
                 .strftime("%Y-%m-%d  %H:%M:%S"))
        self._update_book(px)
        self.draw_chart(force=True)
        self.refresh_orders(force=True)
        self.replay_job = self.frame.after(REPLAY_STEP_MS, self._step_replay)

    # ----------------------------------------------------------------
    def on_quote(self, data):
        if self.replay_mode:
            self.last_quote = data
            return

        first_load = (self.ticker is None)
        self.last_quote = data
        price = data["price"]

        if first_load:
            self.ticker = LiveTicker(self.symbol, price, data["change"])
            self.prices.clear()
            if data.get("history"):
                self.prices.extend(data["history"])
        else:
            self.ticker.set_anchor(price, data["change"])

        self.app.last_prices[self.symbol] = self.ticker.price
        self.app.broker.process(self.symbol, self.ticker.price)
        self.app.check_alerts(self.symbol, self.ticker.price)

        self.time_label.config(
            text=data["timestamp"].strftime("%Y-%m-%d  %H:%M:%S"))
        self.status_lbl.config(text="● LIVE", fg=COLORS["up"])

        self._render_tick(force_chart=True)

        self._news_tick += 1
        if self._news_tick == 1 or self._news_tick % 20 == 0:
            self.refresh_news()

    def on_live_tick(self, visible=True):
        if self.replay_mode or self.ticker is None:
            return
        px = self.ticker.tick()
        self.app.last_prices[self.symbol] = px
        self.app.broker.process(self.symbol, px)
        self.app.check_alerts(self.symbol, px)
        if visible:
            self._render_tick(force_chart=False)

    def _tick_only(self):
        """Advance the ticker without touching Tk widgets."""
        if self.replay_mode or self.ticker is None:
            return
        px = self.ticker.tick()
        self.app.last_prices[self.symbol] = px
        self.app.broker.process(self.symbol, px)
        self.app.check_alerts(self.symbol, px)

    def _render_tick(self, force_chart=False):
        if self.ticker is None:
            return
        px = self.ticker.price
        chg = self.ticker.change()
        pct = self.ticker.pct()

        color = COLORS["up"] if chg >= 0 else COLORS["down"]
        arrow = "▲" if chg >= 0 else "▼"

        price_txt = f"${px:,.2f}"
        if price_txt != self._last_price_text:
            self.price_label.config(text=price_txt, fg=color)
            self._last_price_text = price_txt

        chg_txt = f"{arrow} {chg:+.2f}  ({pct:+.2f}%)"
        if chg_txt != self._last_change_text:
            self.change_label.config(text=chg_txt, fg=color)
            self._last_change_text = chg_txt
            self._last_change_color = color

        self.prices.append(px)
        self._update_book(px)
        self.draw_chart(force=force_chart)
        self.refresh_orders(force=False)

    def _update_book(self, price):
        bids, asks = OrderBook.snapshot(price, levels=5)
        key = (
            tuple((p, s) for p, s in bids),
            tuple((p, s) for p, s in asks),
        )
        if key == self._last_book_tuple:
            return
        self._last_book_tuple = key

        for i, row in enumerate(self.book_rows):
            if i < len(bids):
                bp, bs = bids[i]
                row["bid_price"].config(text=f"{bp:,.2f}")
                row["bid_size"].config(text=f"×{bs}")
            if i < len(asks):
                ap, as_ = asks[i]
                row["ask_price"].config(text=f"{ap:,.2f}")
                row["ask_size"].config(text=f"×{as_}")
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = asks[0][0] - bids[0][0]
            self.book_label.config(
                text=f"BID {bids[0][0]:.2f}  ASK {asks[0][0]:.2f}  "
                     f"SPREAD {spread:.2f}")
            self.mid_label.config(
                text=f"MID ${mid:,.2f}   ·   SPREAD ${spread:.2f}")

    def on_error(self, exc):
        self.status_lbl.config(text=f"● ERROR: {str(exc)[:50]}",
                               fg=COLORS["down"])

    # ----------------------------------------------------------------
    def refresh_news(self):
        try:
            items = self.app.market.news(self.symbol)
        except Exception as exc:
            self.news_header.config(text=f"News unavailable: {exc}")
            return

        for w in self._news_widgets:
            w.destroy()
        self._news_widgets = []

        if not items:
            self.news_header.config(text="No recent headlines.")
            return

        self.news_header.config(
            text=f"{len(items)} headlines  ·  source: Yahoo Finance")

        for it in items:
            row = tk.Frame(self.news_box, bg=COLORS["panel"])
            row.pack(fill="x", pady=1, padx=2)

            title = it["title"]
            if len(title) > 120:
                title = title[:117] + "…"
            lbl = tk.Label(
                row, text=title, bg=COLORS["panel"],
                fg=COLORS["accent_alt"], font=FONT_NEWS,
                anchor="w", justify="left", wraplength=1100,
                cursor="hand2")
            lbl.pack(fill="x")

            meta = tk.Label(
                row,
                text=f"{it['source']}  ·  {it['published']}",
                bg=COLORS["panel"], fg=COLORS["fg_dim"],
                font=FONT_SMALL, anchor="w")
            meta.pack(fill="x")

            if it["link"]:
                lbl.bind("<Button-1>",
                         lambda e, u=it["link"]: webbrowser.open(u))

            self._news_widgets.extend([row, lbl, meta])

    # ----------------------------------------------------------------
    def draw_chart(self, force=False):
        if not self.prices:
            return
        now = time.time()
        if not force and (now - self._last_chart_draw) * 1000 < CHART_MIN_MS:
            return
        self._last_chart_draw = now

        self.ax.clear()
        self._style_axes()
        values = list(self.prices)
        xs = range(len(values))
        self.ax.fill_between(xs, values, min(values),
                             color=COLORS["accent"], alpha=0.10)
        self.ax.plot(xs, values, linewidth=1.8, color=COLORS["accent"])

        live = self.app.last_prices.get(self.symbol)
        if live is not None:
            self.ax.axhline(live, linestyle="--", linewidth=1,
                            color=COLORS["fg_dim"], alpha=0.7)
            for order in self.app.broker.acct.orders:
                if order.status == "OPEN" and order.symbol == self.symbol:
                    c = COLORS["buy"] if order.side == "BUY" \
                        else COLORS["sell"]
                    self.ax.axhline(order.limit_price, linestyle=":",
                                    linewidth=1.2, color=c, alpha=0.85)

        mode = "replay" if self.replay_mode else "live"
        self.ax.set_title(f"{self.symbol}  ·  {mode} feed",
                          color=COLORS["fg"], fontsize=11, loc="left")
        self.canvas.draw_idle()

    # ----------------------------------------------------------------
    def refresh_orders(self, force=False):
        mine = [o for o in self.app.broker.acct.orders
                if o.symbol == self.symbol]
        mine = mine[-150:]
        live = self.app.last_prices.get(self.symbol)

        row_ids = tuple(o.id for o in mine)
        current_ids = tuple(self.orders.get_children())
        if force or row_ids != current_ids:
            self._rebuild_orders(mine, live)
            return

        for o in mine:
            iid = o.id
            prev = self._last_order_row_state.get(iid)
            new_state = self._order_state(o, live)
            if prev == new_state:
                continue
            self.orders.item(iid, values=new_state["values"],
                             tags=new_state["tags"])
            self._last_order_row_state[iid] = new_state

    def _order_state(self, order, live):
        status_txt = order.status
        gap_txt = "—"

        if order.status == "OPEN" and live is not None:
            gap_abs = ((live - order.limit_price)
                       if order.side == "BUY"
                       else (order.limit_price - live))
            gap_pct = (gap_abs / live * 100) if live else 0.0
            gap_txt = f"${gap_abs:+.2f} ({gap_pct:+.2f}%)"
            in_range = (
                (order.side == "BUY" and live <= order.limit_price) or
                (order.side == "SELL" and live >= order.limit_price)
            )
            if in_range:
                status_txt = "IN RANGE"

        tags = [order.side]
        if order.status == "FILLED":
            tags.append("FILLED")
        elif order.status == "REJECTED":
            tags.append("REJECTED")
        elif order.status == "CANCELLED":
            tags.append("CANCELLED")
        elif order.status == "PENDING_PARENT":
            tags.append("PENDING_PARENT")
        if order.status == "OPEN" and live is not None and \
           status_txt == "IN RANGE":
            tags.append("INRANGE")

        pnl_txt = "—"
        if order.status == "FILLED" and order.side == "SELL":
            pnl_txt = f"${order.pnl:+,.2f}"

        values = (
            order.id, order.side, order.shares,
            f"${order.limit_price:,.2f}",
            f"${live:,.2f}" if live is not None else "—",
            gap_txt, status_txt,
            f"${order.filled_price:,.2f}" if order.filled_price else "—",
            pnl_txt, order.created, order.note,
        )
        return {"values": values, "tags": tuple(tags)}

    def _rebuild_orders(self, mine, live):
        self.orders.delete(*self.orders.get_children())
        self._last_order_row_state.clear()
        for o in mine:
            state = self._order_state(o, live)
            self.orders.insert("", "end", iid=o.id,
                               values=state["values"],
                               tags=state["tags"])
            self._last_order_row_state[o.id] = state

    def update_account(self):
        live = self.app.last_prices.get(self.symbol)
        if live is None:
            return
        pos = self.app.broker.position(self.symbol)
        mv = pos.shares * live
        unrealized = self.app.broker.unrealized(self.symbol, live)

        def sv(key, text, color=None):
            if self._acct_last.get(key) == (text, color):
                return
            lbl = self.acct_values[key]
            lbl.config(text=text)
            if color:
                lbl.config(foreground=color)
            self._acct_last[key] = (text, color)

        sv("pos", f"{pos.shares:,} sh")
        sv("avg", f"${pos.avg_price:,.2f}")
        sv("mv",  f"${mv:,.2f}")
        sv("unrealized", f"${unrealized:+,.2f}",
           COLORS["up"] if unrealized >= 0 else COLORS["down"])


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------

class DashboardTab:
    def __init__(self, app, notebook):
        self.app = app
        self.frame = ttk.Frame(notebook, style="TFrame")
        notebook.add(self.frame, text="  DASHBOARD  ")
        self._last_pending_ids = ()
        self._last_pos_rows = {}
        self._last_blotter_len = 0
        self._build()

    def _build(self):
        top = ttk.Frame(self.frame, style="TFrame",
                        padding=(14, 12, 14, 6))
        top.pack(fill="x")
        for i in range(4):
            top.columnconfigure(i, weight=1, uniform="m")

        self.card_equity = MetricCard(top, "Total account value",
                                      accent=COLORS["accent"])
        self.card_equity.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.card_cash = MetricCard(top, "Cash available")
        self.card_cash.grid(row=0, column=1, sticky="nsew", padx=6)
        self.card_positions = MetricCard(top, "Positions market value")
        self.card_positions.grid(row=0, column=2, sticky="nsew", padx=6)
        self.card_profit = MetricCard(top, "Total profit / loss",
                                      accent=COLORS["accent_alt"])
        self.card_profit.grid(row=0, column=3, sticky="nsew", padx=(6, 0))

        second = ttk.Frame(self.frame, style="TFrame",
                           padding=(14, 0, 14, 6))
        second.pack(fill="x")
        for i in range(4):
            second.columnconfigure(i, weight=1, uniform="m2")
        self.card_revenue = MetricCard(second, "Total revenue (sold)",
                                       sublabel="Gross proceeds from sells")
        self.card_revenue.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.card_bought = MetricCard(second, "Total capital deployed",
                                      sublabel="Gross spent on buys")
        self.card_bought.grid(row=0, column=1, sticky="nsew", padx=6)
        self.card_realized = MetricCard(second, "Realized P/L")
        self.card_realized.grid(row=0, column=2, sticky="nsew", padx=6)
        self.card_unreal = MetricCard(second, "Unrealized P/L")
        self.card_unreal.grid(row=0, column=3, sticky="nsew", padx=(6, 0))

        body = tk.PanedWindow(self.frame, orient="vertical",
                              bg=COLORS["bg"], sashwidth=6,
                              sashrelief="flat", borderwidth=0, bd=0)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        pending_panel = ttk.Labelframe(
            body,
            text=" Live Pending Orders  ·  right-click to cancel ",
            style="TLabelframe")
        body.add(pending_panel, minsize=170, height=210, stretch="always")
        p_cols = ("id", "symbol", "side", "shares", "cap", "live",
                  "gap", "gap_pct", "status", "age", "note")
        p_head = {"id": "ID", "symbol": "SYMBOL", "side": "SIDE",
                  "shares": "SHARES", "cap": "CAP", "live": "LIVE PRICE",
                  "gap": "GAP $", "gap_pct": "GAP %",
                  "status": "STATUS", "age": "AGE", "note": "NOTE"}
        pc = ttk.Frame(pending_panel, style="Panel.TFrame")
        pc.pack(fill="both", expand=True, padx=8, pady=8)
        self.pending_tree = ttk.Treeview(pc, columns=p_cols,
                                         show="headings", height=6)
        for col in p_cols:
            self.pending_tree.heading(col, text=p_head[col])
            self.pending_tree.column(col, width=105, anchor="center")
        pvsb = ttk.Scrollbar(pc, orient="vertical",
                             command=self.pending_tree.yview)
        self.pending_tree.configure(yscrollcommand=pvsb.set)
        self.pending_tree.pack(side="left", fill="both", expand=True)
        pvsb.pack(side="right", fill="y")
        self.pending_tree.tag_configure("BUY", foreground=COLORS["buy"])
        self.pending_tree.tag_configure("SELL", foreground=COLORS["sell"])
        self.pending_tree.tag_configure("INRANGE",
                                        background=COLORS["flash"])
        self.pending_tree.tag_configure("PENDING_PARENT",
                                        foreground=COLORS["warn"])
        self.pending_tree.bind("<Button-3>", self._cancel_pending)
        self.pending_tree.bind("<Button-2>", self._cancel_pending)

        positions_panel = ttk.Labelframe(body, text=" Live Positions ",
                                         style="TLabelframe")
        body.add(positions_panel, minsize=170, height=210, stretch="always")
        pos_cols = ("symbol", "shares", "avg", "last", "mkt_value",
                    "unrealized", "unrealized_pct")
        pos_head = {"symbol": "SYMBOL", "shares": "SHARES", "avg": "AVG",
                    "last": "LAST", "mkt_value": "MKT VALUE",
                    "unrealized": "UNREALIZED P/L",
                    "unrealized_pct": "RETURN"}
        pc2 = ttk.Frame(positions_panel, style="Panel.TFrame")
        pc2.pack(fill="both", expand=True, padx=8, pady=8)
        self.pos_tree = ttk.Treeview(pc2, columns=pos_cols,
                                     show="headings", height=6)
        for col in pos_cols:
            self.pos_tree.heading(col, text=pos_head[col])
            self.pos_tree.column(col, width=120, anchor="center")
        pvsb2 = ttk.Scrollbar(pc2, orient="vertical",
                              command=self.pos_tree.yview)
        self.pos_tree.configure(yscrollcommand=pvsb2.set)
        self.pos_tree.pack(side="left", fill="both", expand=True)
        pvsb2.pack(side="right", fill="y")
        self.pos_tree.tag_configure("up", foreground=COLORS["up"])
        self.pos_tree.tag_configure("down", foreground=COLORS["down"])

        blotter_panel = ttk.Labelframe(body, text=" Live Trade Blotter ",
                                       style="TLabelframe")
        body.add(blotter_panel, minsize=180, height=240, stretch="always")
        blot_cols = ("time", "symbol", "side", "shares", "price",
                     "value", "pnl", "order_id", "note")
        blot_head = {"time": "TIME", "symbol": "SYMBOL", "side": "SIDE",
                     "shares": "SHARES", "price": "PRICE",
                     "value": "VALUE", "pnl": "REALIZED P/L",
                     "order_id": "ORDER ID", "note": "NOTE"}
        bc = ttk.Frame(blotter_panel, style="Panel.TFrame")
        bc.pack(fill="both", expand=True, padx=8, pady=8)
        self.blot_tree = ttk.Treeview(bc, columns=blot_cols,
                                      show="headings", height=7)
        for col in blot_cols:
            self.blot_tree.heading(col, text=blot_head[col])
            self.blot_tree.column(col, width=105, anchor="center")
        bvsb = ttk.Scrollbar(bc, orient="vertical",
                             command=self.blot_tree.yview)
        self.blot_tree.configure(yscrollcommand=bvsb.set)
        self.blot_tree.pack(side="left", fill="both", expand=True)
        bvsb.pack(side="right", fill="y")
        self.blot_tree.tag_configure("BUY", foreground=COLORS["buy"])
        self.blot_tree.tag_configure("SELL", foreground=COLORS["sell"])

        self.frame.update_idletasks()
        try:
            body.sash_place(0, 0, 210)
            body.sash_place(1, 0, 450)
        except tk.TclError:
            pass

    def _cancel_pending(self, event):
        row = self.pending_tree.identify_row(event.y)
        if not row:
            return
        vals = self.pending_tree.item(row, "values")
        if not vals:
            return
        oid = vals[0]
        for o in self.app.broker.acct.orders:
            if o.id == oid and o.status in ("OPEN", "PENDING_PARENT"):
                if messagebox.askyesno(
                    "Cancel order",
                    f"Cancel {oid}?\n{o.side} {o.shares} {o.symbol} "
                    f"@ ${o.limit_price:.2f}",
                ):
                    self.app.broker.cancel(oid)
                    self.app.refresh_all_views(force=True)
                    self.app.persist()
                return

    def refresh(self, prices):
        b = self.app.broker
        a = b.acct
        mv = b.market_value(prices)
        equity = a.cash + mv
        realized = a.realized_pnl
        unreal = b.total_unrealized(prices)
        total_pl = realized + unreal
        starting = a.starting_cash

        pl_pct = (total_pl / starting * 100) if starting else 0.0
        acct_pct = ((equity - starting) / starting * 100) \
            if starting else 0.0
        acct_c = COLORS["up"] if equity >= starting else COLORS["down"]

        self.card_equity.set(
            f"${equity:,.2f}", acct_c,
            f"{acct_pct:+.2f}% vs start  ·  acct: {b.current}")
        self.card_cash.set(
            f"${a.cash:,.2f}", COLORS["fg"],
            f"{a.total_fills} fills · {len(b.open_orders())} open")
        self.card_positions.set(
            f"${mv:,.2f}", COLORS["fg"],
            f"{sum(1 for p in a.positions.values() if p.shares)} "
            f"open positions")
        pl_c = COLORS["up"] if total_pl >= 0 else COLORS["down"]
        self.card_profit.set(
            f"${total_pl:+,.2f}", pl_c,
            f"{pl_pct:+.2f}% of starting capital")
        self.card_revenue.set(
            f"${a.total_sold:,.2f}", COLORS["fg"],
            f"from {sum(1 for o in b.fills() if o.side == 'SELL')} "
            f"sell fills")
        self.card_bought.set(
            f"${a.total_bought:,.2f}", COLORS["fg"],
            f"from {sum(1 for o in b.fills() if o.side == 'BUY')} "
            f"buy fills")
        self.card_realized.set(
            f"${realized:+,.2f}",
            COLORS["up"] if realized >= 0 else COLORS["down"],
            "locked in from closed trades")
        self.card_unreal.set(
            f"${unreal:+,.2f}",
            COLORS["up"] if unreal >= 0 else COLORS["down"],
            "open positions, mark-to-market")

        self._refresh_pending(prices)
        self._refresh_positions(prices)
        self._refresh_blotter()

    def _refresh_pending(self, prices):
        open_orders = self.app.broker.open_orders()
        row_ids = tuple(o.id for o in open_orders)
        now = time.time()

        if row_ids != self._last_pending_ids:
            self.pending_tree.delete(*self.pending_tree.get_children())
            self._last_pending_ids = row_ids

        for o in open_orders:
            live = prices.get(o.symbol)
            tags = [o.side]
            if o.status == "PENDING_PARENT":
                tags.append("PENDING_PARENT")
            if live is not None:
                gap_abs = ((live - o.limit_price) if o.side == "BUY"
                           else (o.limit_price - live))
                gap_pct = (gap_abs / live * 100) if live else 0.0
                in_range = (
                    (o.side == "BUY" and live <= o.limit_price) or
                    (o.side == "SELL" and live >= o.limit_price)
                )
                status = ("PENDING PARENT"
                          if o.status == "PENDING_PARENT"
                          else ("IN RANGE" if in_range else "WAITING"))
                if in_range and o.status == "OPEN":
                    tags.append("INRANGE")
                live_txt = f"${live:,.2f}"
                gap_txt = f"${gap_abs:+.2f}"
                gapp_txt = f"{gap_pct:+.2f}%"
            else:
                live_txt = gap_txt = gapp_txt = "—"
                status = ("PENDING PARENT"
                          if o.status == "PENDING_PARENT" else "WAITING")
            age_s = max(0, int(now - (o.created_ts or now)))
            age_txt = (f"{age_s}s" if age_s < 60 else
                       f"{age_s // 60}m {age_s % 60}s" if age_s < 3600 else
                       f"{age_s // 3600}h {(age_s % 3600) // 60}m")
            values = (o.id, o.symbol, o.side, f"{o.shares:,}",
                      f"${o.limit_price:,.2f}",
                      live_txt, gap_txt, gapp_txt,
                      status, age_txt, o.note)
            if self.pending_tree.exists(o.id):
                self.pending_tree.item(o.id, values=values, tags=tags)
            else:
                self.pending_tree.insert("", "end", iid=o.id,
                                         values=values, tags=tags)

    def _refresh_positions(self, prices):
        a = self.app.broker.acct
        row_ids = tuple(sym for sym, pos in a.positions.items()
                        if pos.shares)
        existing = tuple(self.pos_tree.get_children())
        if row_ids != existing:
            self.pos_tree.delete(*self.pos_tree.get_children())
            for sym in row_ids:
                self.pos_tree.insert("", "end", iid=sym, values=("",))
        for sym in row_ids:
            pos = a.positions[sym]
            px = prices.get(sym, pos.avg_price)
            mv_pos = pos.shares * px
            upl = (px - pos.avg_price) * pos.shares
            upl_pct = (((px - pos.avg_price) / pos.avg_price * 100)
                       if pos.avg_price else 0.0)
            tag = "up" if upl >= 0 else "down"
            self.pos_tree.item(
                sym,
                values=(sym, f"{pos.shares:,}",
                        f"${pos.avg_price:,.2f}", f"${px:,.2f}",
                        f"${mv_pos:,.2f}", f"${upl:+,.2f}",
                        f"{upl_pct:+.2f}%"),
                tags=(tag,))

    def _refresh_blotter(self):
        fills = self.app.broker.fills()
        n = len(fills)
        if n == self._last_blotter_len:
            return
        self._last_blotter_len = n
        self.blot_tree.delete(*self.blot_tree.get_children())
        for o in reversed(fills[-250:]):
            value = o.filled_price * o.shares
            pnl_txt = f"${o.pnl:+,.2f}" if o.side == "SELL" else "—"
            self.blot_tree.insert(
                "", "end", iid=o.id,
                values=(o.filled_at or o.created, o.symbol, o.side,
                        f"{o.shares:,}", f"${o.filled_price:,.2f}",
                        f"${value:,.2f}", pnl_txt, o.id, o.note),
                tags=(o.side,))


# ---------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------

class TradingApp:
    def __init__(self, root, symbols, cash, use_persist=True):
        self.root = root
        self.running = True
        self.market = MarketData()
        self.use_persist = use_persist

        broker = load_state() if use_persist else None
        self.broker = broker or Broker(cash)
        if broker is None and cash:
            self.broker.accounts["default"].cash = cash
            self.broker.accounts["default"].starting_cash = cash

        self.tabs = {}
        self.last_prices = {}
        self._alert_flash_job = None
        self._last_dashboard_refresh = 0.0
        self._needs_full_refresh = False

        # Background fetch worker
        self._fetch_q = queue.Queue()
        self._worker = threading.Thread(target=self._fetch_worker,
                                        daemon=True)
        self._worker.start()

        self.root.title("E*TRADE Real-Time Sandbox  ·  Pro")
        self.root.geometry("1620x1020")
        self.root.minsize(1200, 800)
        self.root.configure(bg=COLORS["bg"])

        self._configure_style()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.close)

        for sym in symbols:
            self.open_symbol(sym)

        for sym in self.broker.acct.positions:
            if sym not in self.tabs:
                self.open_symbol(sym)

        self._live_tick()
        self._quote_poll()
        self._dashboard_tick()

    # ----------------------------------------------------------------
    def _configure_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=COLORS["bg"],
                        foreground=COLORS["fg"],
                        fieldbackground=COLORS["panel"], font=FONT_BODY)
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("PanelAlt.TFrame", background=COLORS["panel_alt"])
        style.configure("TLabel", background=COLORS["bg"],
                        foreground=COLORS["fg"])
        style.configure("Panel.TLabel", background=COLORS["panel"],
                        foreground=COLORS["fg"])
        style.configure("Dim.TLabel", background=COLORS["panel"],
                        foreground=COLORS["fg_dim"])
        style.configure("H1.TLabel", background=COLORS["panel"],
                        foreground=COLORS["fg"], font=FONT_H1)
        style.configure("H2.TLabel", background=COLORS["panel"],
                        foreground=COLORS["fg_dim"], font=FONT_H2)
        style.configure("TEntry", fieldbackground=COLORS["panel_alt"],
                        foreground=COLORS["fg"],
                        bordercolor=COLORS["border"],
                        insertcolor=COLORS["fg"], padding=6)
        style.configure("Buy.TButton", background=COLORS["buy"],
                        foreground="#0b1a12", font=FONT_H2,
                        borderwidth=0, padding=10)
        style.map("Buy.TButton",
                  background=[("active", "#1fa96c"),
                              ("pressed", "#178a58")])
        style.configure("Sell.TButton", background=COLORS["sell"],
                        foreground="#1a0b0e", font=FONT_H2,
                        borderwidth=0, padding=10)
        style.map("Sell.TButton",
                  background=[("active", "#e04a59"),
                              ("pressed", "#c13e4c")])
        style.configure("Accent.TButton", background=COLORS["accent"],
                        foreground="#ffffff", font=FONT_H2,
                        borderwidth=0, padding=(14, 8))
        style.map("Accent.TButton",
                  background=[("active", COLORS["accent_alt"]),
                              ("pressed", "#3a6fd8")])
        style.configure("Danger.TButton", background=COLORS["down"],
                        foreground="#1a0b0e", font=FONT_H2,
                        borderwidth=0, padding=8)
        style.map("Danger.TButton",
                  background=[("active", "#e04a59"),
                              ("pressed", "#c13e4c")])
        style.configure("TButton", background=COLORS["panel_alt"],
                        foreground=COLORS["fg"], padding=6, borderwidth=0)
        style.map("TButton",
                  background=[("active", COLORS["accent"]),
                              ("pressed", COLORS["accent_alt"])])
        style.configure("TSeparator", background=COLORS["border"])
        style.configure("Treeview", background=COLORS["panel"],
                        fieldbackground=COLORS["panel"],
                        foreground=COLORS["fg"], rowheight=26,
                        borderwidth=0)
        style.configure("Treeview.Heading",
                        background=COLORS["panel_alt"],
                        foreground=COLORS["fg_dim"],
                        font=FONT_SMALL, borderwidth=0)
        style.map("Treeview",
                  background=[("selected", COLORS["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("TLabelframe", background=COLORS["panel"],
                        foreground=COLORS["fg_dim"],
                        bordercolor=COLORS["border"],
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=COLORS["panel"],
                        foreground=COLORS["accent_alt"], font=FONT_H2)
        style.configure("Vertical.TScrollbar",
                        background=COLORS["panel_alt"],
                        troughcolor=COLORS["bg_alt"],
                        bordercolor=COLORS["bg_alt"],
                        arrowcolor=COLORS["fg_dim"])
        style.map("Vertical.TScrollbar",
                  background=[("active", COLORS["accent"])])
        style.configure("TNotebook", background=COLORS["bg"],
                        borderwidth=0, tabmargins=(6, 6, 6, 0))
        style.configure("TNotebook.Tab", background=COLORS["panel_alt"],
                        foreground=COLORS["fg_dim"], padding=(14, 8),
                        borderwidth=0, font=FONT_H2)
        style.map("TNotebook.Tab",
                  background=[("selected", COLORS["accent"]),
                              ("active", COLORS["panel"])],
                  foreground=[("selected", "#ffffff"),
                              ("active", COLORS["fg"])])

    # ----------------------------------------------------------------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(header, text="E*TRADE", bg=COLORS["bg"],
                 fg=COLORS["accent"], font=(FONT, 18, "bold")
                 ).pack(side="left")
        tk.Label(header, text="  REAL-TIME SANDBOX  ·  PRO",
                 bg=COLORS["bg"], fg=COLORS["fg_dim"],
                 font=(FONT, 12, "bold")).pack(side="left", pady=(3, 0))

        acct_box = tk.Frame(header, bg=COLORS["bg"])
        acct_box.pack(side="left", padx=(20, 0))
        tk.Label(acct_box, text="ACCOUNT", bg=COLORS["bg"],
                 fg=COLORS["fg_dim"], font=FONT_LABEL).pack(side="left")
        self.acct_var = tk.StringVar(value=self.broker.current)
        self.acct_combo = ttk.Combobox(
            acct_box, textvariable=self.acct_var, width=12,
            values=list(self.broker.accounts.keys()), state="readonly")
        self.acct_combo.pack(side="left", padx=6)
        self.acct_combo.bind("<<ComboboxSelected>>",
                             lambda e: self._switch_account())
        ttk.Button(acct_box, text="+ NEW",
                   command=self._new_account).pack(side="left", padx=(0, 4))

        self.alert_banner = tk.Label(header, text="", bg=COLORS["bg"],
                                     fg=COLORS["warn"],
                                     font=(FONT, 10, "bold"))
        self.alert_banner.pack(side="right", padx=10)

        self.global_status = tk.Label(header, text="● READY",
                                      bg=COLORS["bg"], fg=COLORS["up"],
                                      font=(FONT, 9, "bold"))
        self.global_status.pack(side="right", pady=(6, 0), padx=10)

        lookup = tk.Frame(self.root, bg=COLORS["bg_alt"])
        lookup.pack(fill="x", padx=14, pady=(4, 8))

        tk.Label(lookup, text="LOOK UP TICKER", bg=COLORS["bg_alt"],
                 fg=COLORS["fg_dim"], font=(FONT, 9, "bold")
                 ).pack(side="left", padx=(14, 10), pady=10)

        self.symbol_var = tk.StringVar()
        self.lookup_entry = ttk.Entry(lookup, textvariable=self.symbol_var,
                                      width=14, font=(FONT, 12, "bold"))
        self.lookup_entry.pack(side="left", padx=(0, 8), pady=8)
        self.lookup_entry.bind("<Return>", lambda e: self.lookup())
        self.lookup_entry.focus_set()

        ttk.Button(lookup, text="LOOK UP", style="Accent.TButton",
                   command=self.lookup).pack(side="left",
                                             padx=(0, 8), pady=8)
        ttk.Button(lookup, text="CLOSE TAB",
                   command=self.close_current_tab).pack(side="left", pady=8)
        ttk.Button(lookup, text="EXPORT CSV",
                   command=self.export_csv).pack(side="left", padx=(8, 0))
        ttk.Button(lookup, text="PANIC CLOSE", style="Danger.TButton",
                   command=self.panic_close).pack(side="left", padx=(8, 0))

        tk.Label(lookup,
                 text="Ctrl+K lookup · right-click order to cancel · "
                      "bottom bar accepts commands",
                 bg=COLORS["bg_alt"], fg=COLORS["fg_dim"],
                 font=FONT_SMALL).pack(side="right", padx=14)

        self.notebook = ttk.Notebook(self.root, style="TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.dashboard = DashboardTab(self, self.notebook)
        self.notebook.bind("<Button-2>", self._middle_click_close)
        self.notebook.bind("<Button-3>", self._middle_click_close)
        self.notebook.bind("<<NotebookTabChanged>>",
                           lambda e: self._on_tab_changed())

        cmd = tk.Frame(self.root, bg=COLORS["bg_alt"])
        cmd.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(cmd, text="⌘", bg=COLORS["bg_alt"],
                 fg=COLORS["accent"], font=(FONT, 12, "bold")
                 ).pack(side="left", padx=(14, 8), pady=8)
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(cmd, textvariable=self.cmd_var,
                                   font=(FONT_MONO, 10))
        self.cmd_entry.pack(side="left", fill="x", expand=True,
                            padx=(0, 8), pady=8)
        self.cmd_entry.bind("<Return>", lambda e: self._run_command())
        tk.Label(cmd,
                 text="e.g.  AAPL b 100 178.5   |   AAPL s 50 190   |   "
                      "cancel 3F9A21C4   |   close AAPL   |   "
                      "alert AAPL > 200",
                 bg=COLORS["bg_alt"], fg=COLORS["fg_dim"],
                 font=FONT_SMALL).pack(side="left", padx=(0, 14))

        self.root.bind("<Control-k>",
                       lambda e: self.lookup_entry.focus_set())
        self.root.bind("<Control-s>", lambda e: self.persist())

    # ----------------------------------------------------------------
    def _on_tab_changed(self):
        tab = self._current_symbol_tab()
        if tab:
            tab._last_chart_draw = 0.0
            tab.refresh_orders(force=True)
            tab.draw_chart(force=True)

    def _current_symbol_tab(self):
        try:
            current = self.notebook.select()
        except tk.TclError:
            return None
        for tab in self.tabs.values():
            if str(tab.frame) == current:
                return tab
        return None

    # ----------------------------------------------------------------
    def lookup(self):
        raw = self.symbol_var.get().strip().upper()
        if not raw:
            return
        self.symbol_var.set("")
        self.open_symbol(raw)
        if raw in self.tabs:
            self.notebook.select(self.tabs[raw].frame)

    def open_symbol(self, symbol):
        symbol = symbol.strip().upper()
        if not symbol:
            return
        if symbol in self.tabs:
            self.notebook.select(self.tabs[symbol].frame)
            return
        tab = SymbolTab(self, self.notebook, symbol)
        self.tabs[symbol] = tab
        self.notebook.select(tab.frame)
        self._request_quote(symbol, full=True)

    def close_current_tab(self):
        current = self.notebook.select()
        if not current or current == str(self.dashboard.frame):
            return
        for sym, tab in list(self.tabs.items()):
            if str(tab.frame) == current:
                self.notebook.forget(tab.frame)
                tab.frame.destroy()
                del self.tabs[sym]
                break

    def _middle_click_close(self, event):
        try:
            idx = self.notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        tab_id = self.notebook.tabs()[idx]
        if tab_id == str(self.dashboard.frame):
            return
        for sym, tab in list(self.tabs.items()):
            if str(tab.frame) == tab_id:
                self.notebook.forget(tab.frame)
                tab.frame.destroy()
                del self.tabs[sym]
                break

    # ----------------------------------------------------------------
    def _switch_account(self):
        self.broker.set_current(self.acct_var.get())
        for tab in self.tabs.values():
            tab.refresh_orders(force=True)
            tab._refresh_alerts()
        self.refresh_all_views(force=True)
        self.persist()

    def _new_account(self):
        name = simpledialog.askstring("New account", "Account name:",
                                      parent=self.root)
        if not name:
            return
        cash = simpledialog.askfloat("New account", "Starting cash:",
                                     initialvalue=10000.0,
                                     parent=self.root)
        if not cash:
            return
        self.broker.add_account(name, cash)
        self.acct_combo.configure(values=list(self.broker.accounts.keys()))
        self.broker.set_current(name)
        self.acct_var.set(name)
        self.refresh_all_views(force=True)
        self.persist()

    # ----------------------------------------------------------------
    def _run_command(self):
        raw = self.cmd_var.get().strip()
        if not raw:
            return
        self.cmd_var.set("")
        try:
            parts = raw.split()
            verb = parts[0].lower()

            if verb == "cancel" and len(parts) >= 2:
                oid = parts[1].upper()
                if self.broker.cancel(oid):
                    self._notify(f"cancelled {oid}")
                else:
                    self._notify(f"order {oid} not found / not open")
                self.refresh_all_views(force=True)
                self.persist()
                return

            if verb == "close" and len(parts) >= 2:
                sym = parts[1].upper()
                if sym in self.tabs:
                    self.notebook.forget(self.tabs[sym].frame)
                    self.tabs[sym].frame.destroy()
                    del self.tabs[sym]
                return

            if verb == "alert" and len(parts) >= 4:
                sym = parts[1].upper()
                op = parts[2]
                px = float(parts[3])
                if op not in (">", "<"):
                    raise ValueError("op must be > or <")
                a = Alert(id=uuid.uuid4().hex[:6].upper(),
                          symbol=sym, op=op, price=px,
                          created=datetime.now().strftime("%H:%M:%S"))
                self.broker.acct.alerts.append(a)
                if sym in self.tabs:
                    self.tabs[sym]._refresh_alerts()
                self._notify(f"alert {sym} {op} {px:.2f}")
                self.persist()
                return

            if len(parts) >= 2 and parts[1].lower() in (
                    "b", "s", "buy", "sell"):
                sym = parts[0].upper()
                side = "BUY" if parts[1].lower() in ("b", "buy") else "SELL"
                if len(parts) < 4:
                    raise ValueError("usage: SYM b|s shares price")
                shares = int(parts[2])
                price = float(parts[3])
                self.broker.submit_limit(sym, side, shares, price)
                if sym not in self.tabs:
                    self.open_symbol(sym)
                self.tabs[sym].refresh_orders(force=True)
                self.refresh_all_views(force=True)
                self.persist()
                self._notify(f"{side} {shares} {sym} @ {price:.2f}")
                return

            raise ValueError(f"unrecognized command: {raw}")
        except Exception as e:
            messagebox.showerror("Command error", str(e))

    def _notify(self, msg):
        self.global_status.config(text=f"● {msg.upper()}",
                                  fg=COLORS["accent"])
        self.root.after(3000, lambda: self.global_status.config(
            text=f"● {len(self.tabs)} SYMBOL(S) LIVE", fg=COLORS["up"]))

    # ----------------------------------------------------------------
    def panic_close(self):
        if not messagebox.askyesno(
            "PANIC CLOSE",
            "Cancel ALL open orders and market-sell ALL positions?\n\n"
            "This is a paper account — no real orders are sent.",
        ):
            return
        n = self.broker.panic_close(self.last_prices)
        self.refresh_all_views(force=True)
        self.persist()
        messagebox.showinfo("Panic close", f"Closed {n} position(s).")

    # ----------------------------------------------------------------
    def check_alerts(self, symbol, price):
        fired = []
        for a in self.broker.acct.alerts:
            if a.fired or a.symbol != symbol:
                continue
            if (a.op == ">" and price > a.price) or \
               (a.op == "<" and price < a.price):
                a.fired = True
                fired.append(a)
        if fired:
            self.persist()
            for a in fired:
                self._flash_alert(
                    f"{a.symbol} {a.op} ${a.price:.2f} "
                    f"(now ${price:.2f})")
            if symbol in self.tabs:
                self.tabs[symbol]._refresh_alerts()

    def _flash_alert(self, text):
        self.alert_banner.config(text=f"🔔 {text}")
        self.root.bell()
        if self._alert_flash_job:
            try:
                self.root.after_cancel(self._alert_flash_job)
            except Exception:
                pass
        self._alert_flash_job = self.root.after(
            8000, lambda: self.alert_banner.config(text=""))

    # ----------------------------------------------------------------
    def export_csv(self):
        path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            initialfile="etrade_sandbox_export.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        a = self.broker.acct
        try:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["# ACCOUNT", a.name])
                w.writerow(["# CASH", f"{a.cash:.2f}",
                            "STARTING", f"{a.starting_cash:.2f}",
                            "REALIZED_PNL", f"{a.realized_pnl:.2f}"])
                w.writerow([])
                w.writerow(["# POSITIONS"])
                w.writerow(["symbol", "shares", "avg_price", "last",
                            "mkt_value", "unrealized", "return_pct"])
                for sym, p in sorted(a.positions.items()):
                    if p.shares == 0:
                        continue
                    px = self.last_prices.get(sym, p.avg_price)
                    mv = p.shares * px
                    upl = (px - p.avg_price) * p.shares
                    pct = ((px - p.avg_price) / p.avg_price * 100) \
                        if p.avg_price else 0.0
                    w.writerow([sym, p.shares, f"{p.avg_price:.4f}",
                                f"{px:.4f}", f"{mv:.2f}",
                                f"{upl:.2f}", f"{pct:.4f}"])
                w.writerow([])
                w.writerow(["# ORDERS"])
                w.writerow(["id", "symbol", "side", "shares", "cap",
                            "status", "fill_price", "pnl",
                            "created", "filled_at", "note",
                            "parent_id", "child_id"])
                for o in a.orders:
                    w.writerow([
                        o.id, o.symbol, o.side, o.shares,
                        f"{o.limit_price:.4f}", o.status,
                        f"{o.filled_price:.4f}" if o.filled_price else "",
                        f"{o.pnl:.4f}" if o.pnl else "",
                        o.created, o.filled_at, o.note,
                        o.parent_id, o.child_id])
            messagebox.showinfo("Export done", f"Saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ----------------------------------------------------------------
    # Background fetch worker + safe cross-thread delivery
    # ----------------------------------------------------------------
    def _request_quote(self, symbol, full=False):
        if not self.running:
            return
        try:
            self._fetch_q.put_nowait((symbol, full))
        except queue.Full:
            pass

    def _fetch_worker(self):
        primed = set()
        while self.running:
            try:
                item = self._fetch_q.get(timeout=0.5)
            except queue.Empty:
                continue
            symbol, want_full = item
            if symbol is None:
                break
            full = want_full and (symbol not in primed)
            try:
                data = self.market.quote(symbol, full=full)
                if full:
                    primed.add(symbol)
                self._safe_after(self._deliver, symbol, data)
            except Exception as exc:
                self._safe_after(self._deliver_error, symbol, exc)

        # Drain leftover items so producers don't block on a dead queue.
        try:
            while True:
                self._fetch_q.get_nowait()
        except queue.Empty:
            pass

    def _safe_after(self, func, *args):
        """
        Schedule a Tk callback from the worker thread, but swallow
        RuntimeError/TclError if the main loop is already gone.
        """
        if not self.running:
            return
        try:
            self.root.after(0, lambda: func(*args))
        except (RuntimeError, tk.TclError):
            pass

    # ----------------------------------------------------------------
    def _live_tick(self):
        if not self.running:
            return
        current = self._current_symbol_tab()
        for tab in self.tabs.values():
            if tab is current:
                tab.on_live_tick(visible=True)
            else:
                tab._tick_only()
        self.root.after(LIVE_TICK_MS, self._live_tick)

    def _quote_poll(self):
        if not self.running:
            return
        for sym in list(self.tabs.keys()):
            self._request_quote(sym, full=False)
        for sym, pos in self.broker.acct.positions.items():
            if pos.shares and sym not in self.tabs:
                self._request_quote(sym, full=False)
        self.root.after(QUOTE_POLL_MS, self._quote_poll)

    def _dashboard_tick(self):
        if not self.running:
            return
        self.dashboard.refresh(self.last_prices)
        self.root.after(DASHBOARD_HZ_MS, self._dashboard_tick)

    # ----------------------------------------------------------------
    def _deliver(self, symbol, data):
        if not self.running:
            return
        self.last_prices[symbol] = data["price"]
        tab = self.tabs.get(symbol)
        if tab:
            tab.on_quote(data)
        else:
            if self.broker.process(symbol, data["price"]):
                self._needs_full_refresh = True
            self.check_alerts(symbol, data["price"])
        self.global_status.config(
            text=f"● {len(self.tabs)} SYMBOL(S) LIVE", fg=COLORS["up"])

    def _deliver_error(self, symbol, exc):
        if not self.running:
            return
        tab = self.tabs.get(symbol)
        if tab:
            tab.on_error(exc)
        self.global_status.config(text=f"● ERROR on {symbol}",
                                  fg=COLORS["down"])

    # ----------------------------------------------------------------
    def refresh_all_views(self, force=False):
        for tab in self.tabs.values():
            tab.update_account()
        if force:
            self.dashboard.refresh(self.last_prices)

    def persist(self):
        if self.use_persist:
            save_state(self.broker)

    def close(self):
        # 1) Tell every loop to stop scheduling new work.
        self.running = False
        # 2) Unblock the worker so it can exit promptly.
        try:
            self._fetch_q.put_nowait((None, None))
        except Exception:
            pass
        # 3) Give the worker a brief moment to notice self.running=False.
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)
        # 4) Persist and tear down Tk.
        self.persist()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="AAPL",
                        help="Comma-separated tickers to open on launch.")
    parser.add_argument("--symbol", default=None,
                        help="Single ticker (backwards-compatible).")
    parser.add_argument("--cash", type=float, default=10000.0,
                        help="Starting paper cash (fresh run only).")
    parser.add_argument("--no-persist", action="store_true",
                        help="Don't load or save state.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete saved state and start fresh.")
    args = parser.parse_args()

    if args.reset and STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
            print(f"[reset] removed {STATE_FILE}")
        except Exception as e:
            print(f"[reset] {e}")

    raw = args.symbol if (args.symbol and args.symbols == "AAPL") \
        else args.symbols
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()] \
        or ["AAPL"]

    root = tk.Tk()
    TradingApp(root, symbols, args.cash,
               use_persist=not args.no_persist)
    root.mainloop()


if __name__ == "__main__":
    main()