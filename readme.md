# E*TRADE Real-Time Trading Sandbox — Pro Edition

A single-file, dark-themed desktop **paper-trading workstation** built with
Python, Tkinter, and Matplotlib. It streams near-real-time quotes from a
public market-data feed, hosts a local matching engine that fills your limit
orders as prices cross, aggregates everything into a live dashboard, and
shows recent Yahoo Finance headlines for every symbol you look up.

> ⚠️ **This program does not place real orders.** There is no live E*TRADE
> integration. It is educational software and not investment advice.

---

## Table of contents

- [Feature list](#feature-list)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Windows](#windows)
  - [macOS](#macos)
  - [Linux](#linux)
  - [Verifying the install](#verifying-the-install)
  - [Optional: a launcher](#optional-a-launcher)
- [First run](#first-run)
  - [What happens on first launch](#what-happens-on-first-launch)
  - [Your first paper trade (walkthrough)](#your-first-paper-trade-walkthrough)
  - [Where your data lives](#where-your-data-lives)
  - [Recommended first-run settings](#recommended-first-run-settings)
- [Run](#run)
- [Command-line flags](#command-line-flags)
- [Using the app](#using-the-app)
  - [Dashboard tab](#dashboard-tab)
  - [Symbol tabs](#symbol-tabs)
  - [Lookup bar](#lookup-bar)
  - [Command bar](#command-bar)
  - [Keyboard shortcuts](#keyboard-shortcuts)
- [Order semantics](#order-semantics)
  - [Limit orders](#limit-orders)
  - [Bracket orders](#bracket-orders)
  - [Order lifecycle](#order-lifecycle)
- [Alerts](#alerts)
- [Session replay](#session-replay)
- [News feed](#news-feed)
- [Multi-account](#multi-account)
- [Persistence](#persistence)
- [CSV export](#csv-export)
- [Panic close](#panic-close)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [What this program is *not*](#what-this-program-is-not)
- [Roadmap / ideas](#roadmap--ideas)
- [License](#license)

---

## Feature list

**Market data**
- Yahoo Finance public chart endpoint (1-minute bars, previous close).
- Quotes polled every 15 seconds per symbol, off the UI thread.
- Synthetic order-book ladder (5 levels each side) derived from the last trade.

**Trading**
- BUY limit and SELL limit orders with price-cap semantics.
- Bracket orders: BUY limit + auto-activating SELL limit child.
- Dollar-based sizing: enter `$500` and hit **SIZE** to compute shares.
- Quick-fill buttons: **USE BID** / **USE ASK** to peg a cap to the book.
- Right-click any open order (in the dashboard or a symbol tab) to cancel.
- Panic close: cancel all opens, market-sell all positions at last price.

**Dashboard**
- Total account value, cash available, positions market value.
- Total revenue (gross sell proceeds), total capital deployed (gross buys).
- Realized P/L, unrealized P/L, total profit/loss with % of starting capital.
- Live Pending Orders table with live price, gap to cap ($ and %), age, and
  `IN RANGE` / `WAITING` / `PENDING PARENT` status.
- Live Positions table with mark-to-market, unrealized P/L, and return %.
- Live Trade Blotter (fills, newest first) with realized P/L per sell.

**News**
- Recent Yahoo Finance headlines per symbol, loaded from the public
  per-ticker RSS feed.
- Up to 15 items with publisher name and timestamp.
- Click any headline to open it in your default browser.
- Auto-refreshes every ~5 minutes.

**Layout**
- Whole-tab vertical scrollbar so nothing gets clipped on short windows.
- Draggable split between chart and side rail.
- Draggable splits on the dashboard between pending / positions / blotter.

**Extras**
- Multi-account (each account has its own cash, positions, orders, alerts).
- Persistent state saved to `~/.etrade_sandbox/state.json`.
- CSV export of positions, orders, and account header.
- Price alerts (`>` or `<`) with a flashing banner and system bell.
- Session replay — step through the last ~500 1-minute bars at 2× speed.
- Keyboard command bar and shortcuts.

**Safety**
- Every symbol tab shows a "LOCAL PAPER EXECUTION / NO LIVE E*TRADE ORDERS /
  LIMIT ORDERS ONLY" badge block.
- Panic-close confirmation dialog explicitly says "paper account".

---

## Requirements

- **Python 3.9 or newer** (tested on 3.11 and 3.14).
- Windows 10/11, macOS 11+, or a modern Linux desktop with a working Tk.
- Internet access to:
  - `query1.finance.yahoo.com` (quotes and chart bars)
  - `feeds.finance.yahoo.com` (news RSS)

Python packages:

```
requests
matplotlib
```

Everything else (`tkinter`, `csv`, `json`, `xml.etree.ElementTree`,
`webbrowser`, `threading`, `pathlib`) ships with Python. There is **no
`feedparser` requirement** — the RSS is parsed with the standard library.

> **Linux note:** Tk is not always bundled with Python. Install it separately
> via your package manager (`sudo apt install python3-tk` on Debian/Ubuntu,
> `sudo dnf install python3-tkinter` on Fedora).

### There are no API keys

This is worth stating up front because it's a common question. This program
uses **only public, unauthenticated Yahoo Finance endpoints** — the same
ones any browser hits when you load a Yahoo Finance page. There is no API
key to register for, no OAuth flow to complete, no token to paste, and no
account to link. The first time you run it, it just works.

Everything I said earlier in our conversation about E*TRADE OAuth keys,
sandbox credentials, and the preview→place flow applies only to a
**separate live-trading script** that I explicitly did not build into this
GUI. That script would need keys. This one does not.

---

## Installation

### Windows

1. **Install Python 3.11 or newer.** The current official installer bundles
   Tk, and it's the version this project is tested against. Download from
   [python.org/downloads](https://www.python.org/downloads/).

   On the first installer screen, **check "Add python.exe to PATH"** before
   clicking Install. This is the single most common source of "py is not
   recognized" errors later.

2. **Verify Python works.** Open `cmd` or PowerShell and run:

   ```
   py --version
   ```

   You should see something like `Python 3.11.9`. If `py` isn't found, you
   likely skipped the PATH checkbox — uninstall and reinstall with it
   checked, or use the full path to the interpreter.

3. **Install the two dependencies.** Still in `cmd`:

   ```
   py -m pip install requests matplotlib
   ```

4. **Save the script.** Put `etraderprog.py` somewhere you'll find it, e.g.
   `C:\Users\<you>\Downloads\etraderprog.py`.

5. **Run it:**

   ```
   cd C:\Users\<you>\Downloads
   py etraderprog.py
   ```

### macOS

1. **Install Python 3.11+.** The system Python on macOS does not include Tk.
   The cleanest install is Homebrew:

   ```bash
   brew install python-tk@3.12
   ```

   Replace `3.12` with whatever version Homebrew is currently serving. This
   package pulls in both Python and the Tk bindings.

   Alternatively, download the official installer from
   [python.org/downloads/macos](https://www.python.org/downloads/macos/) —
   those builds include Tk by default.

2. **Verify:**

   ```bash
   python3 --version
   python3 -c "import tkinter; print('tk ok')"
   ```

   If `import tkinter` fails, you have a Python install without Tk. Install
   the Homebrew `python-tk` package and use the Homebrew Python.

3. **Install dependencies:**

   ```bash
   python3 -m pip install requests matplotlib
   ```

4. **Run:**

   ```bash
   cd ~/Downloads
   python3 etraderprog.py
   ```

### Linux

1. **Install Python and Tk.** The exact commands depend on your distro:

   **Debian / Ubuntu / Mint:**

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip python3-tk python3-venv
   ```

   **Fedora / RHEL / CentOS Stream:**

   ```bash
   sudo dnf install python3 python3-pip python3-tkinter
   ```

   **Arch / Manjaro:**

   ```bash
   sudo pacman -S python tk
   ```

2. **Install dependencies into a virtual environment** (recommended on
   Linux to avoid clobbering system packages):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install requests matplotlib
   ```

3. **Run:**

   ```bash
   python etraderprog.py
   ```

   Keep the virtualenv active for the session; deactivate with `deactivate`
   when you're done.

### Verifying the install

Before running the app, you can check that everything the script needs is
importable. From a shell:

```
py -c "import tkinter, requests, matplotlib; print('all good')"
```

On macOS/Linux replace `py` with `python3`. If that prints `all good` with no
traceback, the app will launch. If it errors, the missing module name in the
traceback tells you which install step to redo.

### Optional: a launcher

If you launch frequently, drop a small wrapper next to the script so you
don't have to `cd` every time.

**Windows** — save as `run.bat` next to `etraderprog.py`:

```bat
@echo off
py "%~dp0etraderprog.py" %*
pause
```

Double-click it and the app opens with a console for tracebacks. Any CLI
flags you pass to the .bat are forwarded.

**macOS / Linux** — save as `run.sh` next to the script:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 etraderprog.py "$@"
```

Then `chmod +x run.sh`. Run it with `./run.sh` or `./run.sh --reset`.

---

## First run

### What happens on first launch

The first time you run the script there is no saved state, so:

1. The app creates a directory `~/.etrade_sandbox/` in your home folder
   (Windows: `C:\Users\<you>\.etrade_sandbox\`).
2. It creates one paper account named **default** with **$10,000** starting
   cash. If you passed `--cash 25000` on the command line, the default
   account starts with $25,000 instead.
3. It opens the **DASHBOARD** tab and one tab for **AAPL**.
4. It begins polling Yahoo Finance for the current AAPL price, and it fetches
   the AAPL news feed.
5. It writes `~/.etrade_sandbox/state.json` on the first significant action
   (order, cancel, alert, or on close).

You should see, within a couple of seconds:

- The header pill changes from `● READY` to `● 1 SYMBOL(S) LIVE` (green).
- The AAPL tab shows a price in the header.
- The chart draws a line of the last ~120 one-minute observations.
- The news panel at the bottom of the AAPL tab populates with headlines.

If any of that doesn't happen, see [Troubleshooting](#troubleshooting).

### Your first paper trade (walkthrough)

This walkthrough places a limit order that fills immediately, so you can
watch the whole pipeline end-to-end.

1. **Open the AAPL tab** (it's already open on first launch).

2. **Read the current price** from the tab header — say it shows
   `$185.42`.

3. **Set a BUY cap that will fill right now.** Enter a cap *above* the last
   price, e.g. `$186.00`. Because the market is already below your cap, the
   order will match on the next poll tick (up to ~15 seconds).

4. **Set shares.** The Shares field defaults to `1`. Leave it, or try the
   dollar-based sizing: type `500` in the `or $ amount` field and click
   **SIZE** — the Shares field updates to `int(500 / 186) = 2`.

5. **Click PLACE BUY LIMIT.** A dialog appears with the order ID. Click OK.

6. **Watch it fill.** Within a few seconds:

   - On the AAPL tab, the order row in the Orders table flips from
     `IN RANGE` to `FILLED`.
   - The dashboard's Pending Orders table drops the row.
   - The dashboard's Positions table gains an AAPL row with your shares and
     average price.
   - The dashboard's Blotter shows a `BUY` row for the fill.
   - The This Symbol panel on the AAPL tab shows `1 sh` (or `2 sh`) and the
     market value.

7. **Now sell it.** In the AAPL tab, enter a SELL cap *below* the last price
   (e.g. `$184.00` if the price is `$185.42`) and click **PLACE SELL LIMIT**.
   It fills on the next tick.

8. **Watch realized P/L appear.** After the sell:

   - The dashboard's Realized P/L card shows the difference between your buy
     fill price and sell fill price, times shares.
   - The Blotter shows the SELL row with a green (profit) or red (loss)
     P/L column.
   - The Positions table drops the AAPL row (position went to zero).

That's the full loop: place → fill → position → exit → P/L. Everything else
in the app is a refinement on this loop.

### Where your data lives

```
~/.etrade_sandbox/
└── state.json
```

| Platform | Full path                                     |
|----------|-----------------------------------------------|
| Windows  | `C:\Users\<you>\.etrade_sandbox\state.json`   |
| macOS    | `/Users/<you>/.etrade_sandbox/state.json`     |
| Linux    | `/home/<you>/.etrade_sandbox/state.json`      |

This file is **plain JSON** and holds every account, position, order, alert,
cash balance, and P/L figure. You can:

- **Inspect it** in any text editor to see exactly what the app tracks.
- **Back it up** by copying it somewhere safe.
- **Reset it** by deleting it (or run with `--reset`).
- **Edit it** by hand — for example, to simulate a deposit, bump the `cash`
  field on your default account.

The app never writes anywhere else. It does not touch your system registry,
does not phone home, and does not upload your data anywhere except the
Yahoo Finance quote and news endpoints it uses to display prices and
headlines.

### Recommended first-run settings

Not required, but worth knowing:

**Starting cash.** `--cash 25000` (or any amount) is only honored on a fresh
run. Once `state.json` exists, the CLI flag is ignored and your persisted
balance wins. To reset, use `--reset` or delete the file. If you want to
change cash mid-session without losing positions or order history, stop the
app and hand-edit the `cash` field of your default account in `state.json`.

**Symbols on launch.** `--symbols AAPL,NVDA,AMD,TSLA` preloads tabs. If you
already hold a position from a previous session, that symbol's tab will open
automatically on next launch even if you don't list it, so you never lose
sight of an open position.

**Ephemeral mode.** `--no-persist` runs the whole app in memory only — good
for demos or for experimenting without polluting your saved state. Nothing is
read and nothing is written. Perfect for a "clean slate" run.

**Multiple paper accounts.** Use `+ NEW` in the header to create a second
account, e.g. `"swing"` with $50,000 and `"scalp"` with $5,000. The dropdown
switches between them; each has its own cash, positions, orders, and alerts.
Useful for testing different strategies without cross-contamination.

**Panel sizing.** The dashboard and symbol tabs are built on draggable
`PanedWindow` sashes. Drag the horizontal dividers between chart / orders /
news to taste — the app remembers nothing about sash positions between
sessions, so set them each time you open it.

**Symbol tab scroll.** If you're on a short laptop screen, the symbol tab
itself has a vertical scrollbar on its right edge. The news panel sits at the
bottom; scroll down to reach it. The right-hand side rail (order book, trade
controls, account, alerts) has its own independent scrollbar.

---

## Run

Basic run — opens with **AAPL** and **$10,000** paper cash:

```bash
py etraderprog.py
```

Preload multiple tabs and a bigger starting balance:

```bash
py etraderprog.py --symbols AAPL,NVDA,AMD,TSLA --cash 25000
```

Fresh start (ignore and overwrite saved state):

```bash
py etraderprog.py --reset
```

Ephemeral session (don't load or save state):

```bash
py etraderprog.py --no-persist
```

---

## Command-line flags

| Flag            | Default   | Meaning                                                              |
|-----------------|-----------|----------------------------------------------------------------------|
| `--symbols`     | `AAPL`    | Comma-separated tickers to open on launch.                          |
| `--symbol`      | *(none)*  | Single ticker. If given, overrides `--symbols`.                     |
| `--cash`        | `10000`   | Starting paper cash, **only used on a fresh run** (no saved state). |
| `--reset`       | off       | Delete `~/.etrade_sandbox/state.json` before launching.             |
| `--no-persist`  | off       | Don't load or save state at all.                                    |

---

## Using the app

### Dashboard tab

Always present and cannot be closed. It has:

**Row 1 — headline metrics**
- Total account value (cash + positions mark-to-market), with `% vs start`.
- Cash available, with `fills · open orders`.
- Positions market value, with count of open positions.
- Total profit/loss (realized + unrealized), with `% of starting capital`.

**Row 2 — revenue & P/L breakdown**
- Total revenue (gross sell proceeds).
- Total capital deployed (gross buy cost).
- Realized P/L (locked-in from closed trades).
- Unrealized P/L (mark-to-market on open positions).

**Three stacked, draggable tables**
1. **Pending Orders** — every `OPEN` or `PENDING_PARENT` order with:
   - `LIVE PRICE` (streaming)
   - `GAP $` and `GAP %` (distance from cap)
   - `STATUS`: `WAITING`, `IN RANGE` (green tint), or `PENDING PARENT` (amber)
   - `AGE` — `45s` / `3m 12s` / `1h 04m`
   - Right-click any row to cancel.
2. **Positions** — symbol, shares, avg, last, market value, unrealized P/L,
   return %, colored green/red.
3. **Blotter** — every fill, newest first, with realized P/L on sells.

### Symbol tabs

One per ticker you look up. The whole tab scrolls vertically, so nothing gets
clipped on short windows. Top to bottom:

**Header strip**
- Symbol, last price (large), change and change %, bid/ask/spread, live/error
  status pill, and a timestamp.

**Replay strip**
- `▶ START REPLAY` button and a mode label (`live mode` / `replay N bars @ 2x`).

**Chart + side rail**
- Left: chart of the last ~120 observations, area fill underneath, dashed
  line for the live price, dotted lines for each open cap on that symbol
  (green buy / red sell).
- Right (own scrollbar):
  - **Order Book** — 5 synthetic bid levels (green) and 5 ask levels (red),
    size chips, mid price line.
  - **Limit Trading**
    - `USE BID` / `USE ASK` quick-fill.
    - Shares field and a dollar-amount field with `SIZE`.
    - BUY cap field and `PLACE BUY LIMIT`.
    - Bracket block: `sell target $` or `sell target %` and `PLACE BRACKET`.
    - SELL cap field and `PLACE SELL LIMIT`.
  - **This Symbol** — position, avg price, market value, unrealized P/L.
  - **Price Alerts** — add `>` / `<` alert, list of active/fired,
    `CLEAR FIRED`.
  - **Safety** — the paper-only badges.

**Orders table**
- Per-symbol orders with ID, side, shares, cap, live, gap to cap, status,
  fill price, P/L, time, note. Right-click to cancel.

**Recent News**
- Up to 15 Yahoo Finance headlines for the ticker, with publisher and
  timestamp. Click a headline to open it in your browser. Refreshes every
  ~5 minutes.

### Lookup bar

- Type a ticker, press **Enter** or click **LOOK UP**.
- Tabs are uppercased automatically and reused if already open.
- **CLOSE TAB** closes the active symbol tab (dashboard is protected).
- Middle-click or right-click any tab to close it.
- **EXPORT CSV** dumps the current account.
- **PANIC CLOSE** cancels everything and market-sells positions.

### Command bar

Type a command at the bottom and press **Enter**.

| Command                     | Meaning                                                          |
|-----------------------------|------------------------------------------------------------------|
| `AAPL b 100 178.5`          | Buy 100 AAPL at limit $178.50.                                   |
| `AAPL s 50 190`             | Sell 50 AAPL at limit $190.00.                                   |
| `cancel 3F9A21C4`           | Cancel order with that ID.                                       |
| `close AAPL`                | Close the AAPL tab (does not touch orders).                      |
| `alert AAPL > 200`          | Add a price alert for AAPL crossing above $200.                  |

Short forms `b` / `buy` and `s` / `sell` are accepted. Alerts also accept `<`.

### Keyboard shortcuts

| Shortcut     | Action                          |
|--------------|---------------------------------|
| `Ctrl+K`     | Focus the lookup entry.         |
| `Ctrl+S`     | Force-save state.               |
| `Enter`      | In lookup or command bar.       |
| Middle-click | Close a symbol tab.             |
| Right-click  | Cancel an order / close a tab.  |

---

## Order semantics

### Limit orders

- **BUY limit** fills when market price **≤** your cap.
- **SELL limit** fills when market price **≥** your cap.

Fills execute at the *current market price* (not your cap), i.e. no
price-improvement simulation.

Cash for open BUY orders is reserved — you cannot submit a BUY whose
`shares × cap` plus already-reserved BUY cash exceeds your cash balance.

Shares for open SELL orders are reserved — you cannot submit a SELL larger
than `position − shares already reserved by open SELLs`.

### Bracket orders

`PLACE BRACKET` submits two linked orders:

1. Parent: `BUY <shares> @ buy_cap`.
2. Child: `SELL <shares> @ target`, where target is either
   `sell_target $` if provided, or `buy_cap × (1 + sell_pct/100)`.

The child sits in status `PENDING PARENT` (amber) until the parent fills, at
which point the child flips to `OPEN` and starts matching.

Cancelling either leg cancels the other.

### Order lifecycle

```
OPEN ──fill──▶ FILLED
  │
  ├──cancel──▶ CANCELLED
  │
  └──insufficient funds at fill time──▶ REJECTED

PENDING_PARENT ──parent fills──▶ OPEN ──▶ …
       │
       └──cancel──▶ CANCELLED
```

---

## Alerts

Add from the symbol tab's Price Alerts panel, or via the command bar
(`alert AAPL > 200`).

- `>` fires when the last price **strictly exceeds** the target.
- `<` fires when the last price **strictly falls below** the target.
- On fire: banner at the top of the window, system bell, entry marked `✓`.
- `CLEAR FIRED` removes fired alerts for that symbol.
- Alerts persist across sessions.

---

## Session replay

On any symbol tab, click **▶ START REPLAY**.

- Uses the last ~500 one-minute bars from the current quote.
- Steps one bar every 500 ms (2× realtime).
- The live feed is paused for that tab while replay is running.
- Orders and alerts still evaluate against the replayed tape, so you can
  practice entries and exits on a real market day.
- Click **■ STOP REPLAY** to return to live mode.

Replay is per-tab; other tabs continue live.

---

## News feed

Each symbol tab loads recent headlines from Yahoo Finance's per-ticker RSS
feed:

```
https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>&region=US&lang=en-US
```

- Parsed with the standard library's `xml.etree.ElementTree` — no extra
  dependency.
- Up to 15 items shown, each with title, publisher, and published date.
- Click a headline to open it in your default browser.
- Refreshes once on tab creation, then every ~20 poll ticks (~5 minutes).
- If Yahoo is throttling or a ticker has no coverage, the panel header shows
  `News unavailable: …` or `No recent headlines.` — the app does not crash.

---

## Multi-account

The account dropdown in the header controls which paper account is active.
Each account has its own cash, positions, orders, alerts, and P/L.

- `+ NEW` prompts for a name and starting cash.
- Switching accounts refreshes all tables and the dashboard.
- All accounts are saved in the same `state.json`.

---

## Persistence

State is written to:

```
~/.etrade_sandbox/state.json
```

That's:

| Platform | Path                                          |
|----------|-----------------------------------------------|
| Windows  | `C:\Users\<you>\.etrade_sandbox\state.json`   |
| macOS    | `/Users/<you>/.etrade_sandbox/state.json`     |
| Linux    | `/home/<you>/.etrade_sandbox/state.json`      |

Save triggers: order placement, cancellation, alert add/clear, panic close,
account switch/new, and on window close. `Ctrl+S` forces a save.

`--reset` deletes the file. `--no-persist` skips load and save.

Because the JSON is plain text, you can edit it by hand if you want to
simulate a deposit or reset a position.

---

## CSV export

Click **EXPORT CSV** in the lookup bar. The file contains three sections:

1. **Account header** — account name, cash, starting cash, realized P/L.
2. **Positions** — symbol, shares, avg, last, market value, unrealized P/L,
   return %.
3. **Orders** — id, symbol, side, shares, cap, status, fill price, P/L,
   created, filled at, note, parent ID, child ID.

Sections are separated by blank rows and prefixed with `#` comment headers so
they open cleanly in Excel, Numbers, or pandas.

---

## Panic close

The red **PANIC CLOSE** button in the lookup bar:

1. Confirms with a dialog (explicitly says "paper account").
2. Cancels every open and pending order.
3. Market-sells every open position at last known price.
4. Logs each close as a `FILLED` order with note `PANIC CLOSE`.
5. Saves state and refreshes all tables.

Useful when you want a flat book without clicking through tabs.

---

## Project layout

Everything is in one file. Internal structure top-to-bottom:

```
Theme (COLORS, FONT_*)
OrderBook.snapshot()      # synthetic depth
MarketData.quote()        # Yahoo chart fetch
MarketData.news()         # Yahoo RSS headlines (stdlib XML parse)
Dataclasses               # Order, Position, Alert, Account
Broker                    # multi-account, matching, persistence, panic close
save_state / load_state   # JSON I/O
ScrollFrame               # canvas + vscrollbar helper
MetricCard                # dashboard card
SymbolTab                 # per-symbol tab (chart, rails, orders, news)
DashboardTab              # dashboard tab
TradingApp                # main window, polling loop, command bar
main()                    # argparse + Tk
```

The symbol tab is laid out top-to-bottom inside a single `ScrollFrame`:

```
ScrollFrame (whole tab)
 ├── Header strip
 ├── Replay strip
 ├── top_split (520px, PanedWindow)
 │    ├── Chart
 │    └── Right rail (ScrollFrame: book / trade / account / alerts / safety)
 ├── Orders block (260px)
 └── News block   (260px)
```

---

## Troubleshooting

**`_tkinter.TclError: unknown option "-fg"`**
This project uses `ttk.Label` in a few places. `ttk` widgets don't accept the
short forms `fg=` / `bg=`; use `foreground=` / `background=` instead. If you
add your own coloring, use the long names.

**Blank chart / "No data for 'XYZ'"**
Yahoo occasionally rate-limits. Wait a few seconds; the app retries every
15 seconds per symbol. Verify the ticker exists on Yahoo Finance in a browser.

**News panel says "News unavailable"**
Yahoo is throttling your IP, or your network blocks the RSS endpoint. Wait a
minute, switch tabs to force a re-fetch, or test the URL directly in a
browser:

```
https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US
```

**News panel says "No recent headlines."**
The ticker has no Yahoo Finance news coverage (common for thinly-traded
symbols, some ETFs, and foreign listings). That's a valid empty result, not
an error.

**Can't see the news panel**
The whole symbol tab scrolls — grab the scrollbar on the right edge of the
tab and drag down. The news panel is the last section.

**Chart is empty on a fresh tab**
First quote may take 1–2 seconds. The status pill in the tab header shows
`● CONNECTING…` until the first reply.

**Order sits at `IN RANGE` but doesn't fill**
The fill runs on the next poll tick (up to 15 seconds). If you want instant
fills, place the order and then move to a symbol tab — the process runs every
time a quote arrives. Bracket children also activate on the poll cycle.

**Persisted state looks wrong**
Close the app, delete `~/.etrade_sandbox/state.json`, and relaunch. Or run
with `--reset`. If you only want to reset cash, edit the file by hand.

**`KeyError` on a state file from an older version**
The dataclasses gained fields over the build. Delete the state file or
`--reset`. If you have real paper history you want to keep, back the file up
first and hand-edit missing fields.

**Windows console flashes and closes**
Run from `cmd` or PowerShell (as in the examples) so you see the traceback.

---

## What this program is *not*

- **Not** a live trading client. It does not and will not send orders to
  E*TRADE or any broker.
- **Not** investment advice.
- **Not** a strategy tester — there's no backtest loop, no indicators, no
  performance statistics beyond P/L.
- **Not** a market-data source. Yahoo's endpoints are public but unofficial
  and can change without notice.

If you actually want to automate E*TRADE orders, do it as a **separate,
headless, audited script** using a mature library (for Python, `wetrade` is
the current recommendation). Test in the E*TRADE sandbox first, then in
production during off-market hours with limit orders far from the market.
Never use market orders for the first automated run.

---

## Roadmap / ideas

Things this build doesn't yet do, but would be natural next steps:

- Sortable treeview columns (click header to sort).
- Row flash animation on fill.
- Filter: "only show orders within 1% of filling".
- Time & sales log (every tick, not just fills).
- Intraday VWAP / simple moving average overlays on the chart.
- Position-level stop-loss / take-profit ladders (multi-leg brackets).
- Named watchlists.
- Dark/light theme switch.
- Optional websocket feed instead of the 15-second polling loop.
- News sentiment tagging (bullish / bearish / neutral) per headline.

---

## License

Educational software. Use at your own risk. No warranty of any kind.
The name "E*TRADE" is used descriptively to reference the brokerage this
sandbox is modeled after; this project is not affiliated with or endorsed
by Morgan Stanley / E*TRADE.