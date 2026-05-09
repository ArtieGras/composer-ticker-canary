import os
import smtplib
import requests
import json
import time
import yfinance as yf
import pandas as pd
import numpy as np
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google import genai

# --- CONFIGURATION (Pulled securely from GitHub Vault) ---
def _get_secret(name):
    """Read an env var, returning empty string if unset or whitespace-only."""
    return (os.environ.get(name) or "").strip()

GMAIL_ADDRESS = _get_secret("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _get_secret("GMAIL_APP_PASSWORD")
ALPACA_API_KEY = _get_secret("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _get_secret("ALPACA_SECRET_KEY")
GEMINI_API_KEY = _get_secret("GEMINI_API_KEY")
EDGAR_USER_AGENT = _get_secret("EDGAR_USER_AGENT")

# Fail-fast validation: all 6 secrets are required. Stops a misconfigured fork
# from running silently with degraded function (e.g. workflow appears to succeed
# but no email arrives because GMAIL_APP_PASSWORD was empty).
_required_secrets = [
    ("GMAIL_ADDRESS", GMAIL_ADDRESS,
     "Your Gmail address (e.g. you@gmail.com)"),
    ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD,
     "Gmail 16-character app password (NOT your regular password)"),
    ("ALPACA_API_KEY", ALPACA_API_KEY,
     "Alpaca paper-trading API Key ID"),
    ("ALPACA_SECRET_KEY", ALPACA_SECRET_KEY,
     "Alpaca paper-trading Secret Key"),
    ("GEMINI_API_KEY", GEMINI_API_KEY,
     "Google Gemini API key (free tier is sufficient)"),
    ("EDGAR_USER_AGENT", EDGAR_USER_AGENT,
     'SEC EDGAR User-Agent in format "Your Name your.email@example.com"'),
]
_missing = [(name, desc) for name, value, desc in _required_secrets if not value]
if _missing:
    _msg_lines = ["ERROR: Required GitHub Secrets are not set or are empty:"]
    for name, desc in _missing:
        _msg_lines.append(f"  - {name}: {desc}")
    _msg_lines += [
        "",
        "Set each as a GitHub Secret in your repo:",
        "  Settings -> Secrets and variables -> Actions -> New repository secret",
        "",
        "See the README for full setup instructions.",
    ]
    raise SystemExit("\n".join(_msg_lines))

# Initialize the GenAI Client (after validation confirms GEMINI_API_KEY is set)
client = genai.Client(api_key=GEMINI_API_KEY)

FILE_PATH = "active_tickers.txt"
PREVIOUS_REGIME_FILE = "previous_regime.json"
PREVIOUS_ASSETS_FILE = "previous_assets.json"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}
BASE_URL = "https://paper-api.alpaca.markets/v2"

# ============================================================
# SEC EDGAR — Advance-notice scanner for delistings, liquidations,
# symbol/name changes. Searches full-text of recent filings.
# ============================================================
EDGAR_BASE_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_HEADERS = {
    "User-Agent": EDGAR_USER_AGENT,
    "Accept": "application/json",
}
# Form types most likely to carry advance closure / reorganization notices.
# 497 / 497K = prospectus supplements (where ETF closures are typically announced).
# 8-K = material events. DEF 14A = proxy (used for reorgs requiring shareholder vote).
EDGAR_FORMS = "497,497K,8-K,DEF 14A"
EDGAR_LOOKBACK_DAYS = 90
# Closure / reorganization keyword set. Joined with ticker via AND.
EDGAR_KEYWORDS = (
    '"liquidation" OR "liquidate" OR "terminate" OR "termination" OR '
    '"ceasing operations" OR "fund closure" OR "final trading day" OR '
    '"wind down" OR "winding down" OR "name change" OR "ticker change" OR '
    '"symbol change" OR "reorganization"'
)
EDGAR_MAX_HITS_PER_TICKER = 5  # cap to avoid noise floods
EDGAR_REQUEST_DELAY = 0.15     # seconds between requests (~6/sec, well under SEC's 10/sec)
# HTTP status codes that indicate a transient SEC backend issue worth retrying once.
# 429 = rate limited. 5xx = SEC backend hiccups (efts.sec.gov is known for transient 500s).
EDGAR_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
EDGAR_RETRY_BACKOFF_SECONDS = 2
# If EDGAR fails on more than this fraction of tickers, surface a degraded-coverage
# warning in the email so silent SEC outages become visible.
EDGAR_DEGRADED_COVERAGE_THRESHOLD = 0.15

# ============================================================
# EXCEPTION THRESHOLDS — Gemini only fires when these trip
# ============================================================
THRESHOLDS = {
    'vix_high': 25.0,           # Elevated fear
    'vix_spike_pct': 20.0,      # Single-day VIX jump (%)
    'yield_curve_inversion': 0, # 10Y-2Y spread <= 0
    'spy_drop_pct': -2.0,       # Big single-day SPY decline (%)
    'spy_rally_pct': 2.5,       # Unusual single-day SPY rally (%)
}

# ============================================================
# MARKET INTERNALS — Pure quant, no AI needed
# ============================================================
def fetch_market_internals():
    """Pull VIX, yield curve spread, and SPY daily move from yfinance."""
    print("Fetching market internals (VIX, yield curve, SPY)...")
    internals = {}

    try:
        # --- VIX ---
        vix = yf.Ticker("^VIX")
        vix_hist = vix.history(period="5d")
        if vix_hist is not None and len(vix_hist) >= 2:
            current_vix = vix_hist['Close'].iloc[-1]
            prev_vix = vix_hist['Close'].iloc[-2]
            vix_change_pct = ((current_vix - prev_vix) / prev_vix) * 100
            internals['vix'] = round(current_vix, 2)
            internals['vix_prev'] = round(prev_vix, 2)
            internals['vix_change_pct'] = round(vix_change_pct, 1)
        else:
            internals['vix'] = None
    except Exception as e:
        print(f"  VIX fetch failed: {e}")
        internals['vix'] = None

    try:
        # --- YIELD CURVE (10Y - 2Y Treasury Spread) ---
        tnx = yf.Ticker("^TNX")  # 10-Year yield
        twoy = yf.Ticker("2YY=F")  # 2-Year yield future (proxy; not cash 2Y)
        tnx_hist = tnx.history(period="5d")
        twoy_hist = twoy.history(period="5d")

        if tnx_hist is not None and not tnx_hist.empty and twoy_hist is not None and not twoy_hist.empty:
            yield_10y = tnx_hist['Close'].iloc[-1]
            yield_2y = twoy_hist['Close'].iloc[-1]
            spread = yield_10y - yield_2y
            internals['yield_10y'] = round(yield_10y, 3)
            internals['yield_2y'] = round(yield_2y, 3)
            internals['yield_spread'] = round(spread, 3)
        else:
            internals['yield_spread'] = None
    except Exception as e:
        print(f"  Yield curve fetch failed: {e}")
        internals['yield_spread'] = None

    try:
        # --- SPY DAILY CHANGE ---
        spy = yf.Ticker("SPY")
        spy_hist = spy.history(period="5d")
        if spy_hist is not None and len(spy_hist) >= 2:
            spy_close = spy_hist['Close'].iloc[-1]
            spy_prev = spy_hist['Close'].iloc[-2]
            spy_change_pct = ((spy_close - spy_prev) / spy_prev) * 100
            internals['spy_price'] = round(spy_close, 2)
            internals['spy_change_pct'] = round(spy_change_pct, 2)
        else:
            internals['spy_price'] = None
            internals['spy_change_pct'] = None
    except Exception as e:
        print(f"  SPY fetch failed: {e}")
        internals['spy_price'] = None
        internals['spy_change_pct'] = None

    return internals


def check_exception_triggers(internals, regime_data):
    """Evaluate whether any metric is outside normal bands. Returns list of triggered alerts."""
    triggers = []

    vix = internals.get('vix')
    if vix is not None:
        if vix >= THRESHOLDS['vix_high']:
            triggers.append(f"VIX ELEVATED: {vix} (threshold: {THRESHOLDS['vix_high']})")
        vix_chg = internals.get('vix_change_pct', 0)
        if abs(vix_chg) >= THRESHOLDS['vix_spike_pct']:
            triggers.append(f"VIX SPIKE: {vix_chg:+.1f}% single-day move")

    spread = internals.get('yield_spread')
    if spread is not None and spread <= THRESHOLDS['yield_curve_inversion']:
        triggers.append(f"YIELD CURVE INVERTED: 10Y-2Y spread = {spread:.3f}")

    spy_chg = internals.get('spy_change_pct')
    if spy_chg is not None:
        if spy_chg <= THRESHOLDS['spy_drop_pct']:
            triggers.append(f"SPY SHARP DROP: {spy_chg:+.2f}% in single session")
        elif spy_chg >= THRESHOLDS['spy_rally_pct']:
            triggers.append(f"SPY UNUSUAL RALLY: {spy_chg:+.2f}% in single session")

    # Check for regime change from previous day
    if regime_data:
        regime_changed = _check_regime_change(regime_data)
        if regime_changed:
            triggers.append(regime_changed)

    return triggers


def _check_regime_change(current_regime):
    """Compare today's regime status against yesterday's. Returns alert string or None."""
    try:
        with open(PREVIOUS_REGIME_FILE, "r") as f:
            prev = json.load(f)
        prev_status = prev.get('status', '')
        curr_status = current_regime.get('status', '')
        if prev_status and curr_status and prev_status != curr_status:
            return f"REGIME CHANGE: {prev_status} → {curr_status}"
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # First run or corrupt file — no comparison possible
    return None


def _save_current_regime(regime_data):
    """Persist today's regime for tomorrow's comparison."""
    if regime_data:
        try:
            with open(PREVIOUS_REGIME_FILE, "w") as f:
                json.dump(regime_data, f)
        except Exception as e:
            print(f"Failed to save regime state: {e}")


# ============================================================
# TECHNICAL REGIME — Multi-factor (200 SMA, ADX, ER)
# ============================================================
def calculate_market_regime(ticker_symbol="SPY"):
    print(f"Calculating multi-factor technical regime for {ticker_symbol}...")
    try:
        ticker = yf.Ticker(ticker_symbol)
        df = ticker.history(period="2y")
        if df is None or df.empty:
            return None

        # Drop incomplete intraday candle ONLY if the market hasn't closed yet.
        # yfinance daily bars are stamped with date only; before 16:00 ET the
        # final bar is partial, after 16:00 ET it's the official close.
        ny_time = datetime.now(ZoneInfo("America/New_York"))
        if ny_time.hour < 16 and df.index[-1].date() == ny_time.date():
            df = df.iloc[:-1]

        if len(df) < 200:
            return None

        close = df['Close']
        high = df['High']
        low = df['Low']

        current_price = close.iloc[-1]
        sma_200 = close.rolling(window=200).mean().iloc[-1]

        # --- Kaufman's Efficiency Ratio (ER) & EMA ---
        n_er = 20
        change = close.diff(n_er).abs()
        volatility = close.diff().abs().rolling(n_er).sum()
        er_series = change / volatility.replace(0, np.nan)
        current_er = er_series.iloc[-1]
        ema_er_series = er_series.ewm(span=10, adjust=False).mean()
        current_ema_er = ema_er_series.iloc[-1]

        # --- Average Directional Index (ADX) ---
        # Wilder's directional movement uses SIGNED differences:
        #   up_move   = current_high - previous_high
        #   down_move = previous_low  - current_low
        # Both can be negative; the comparison logic below requires the sign.
        n_adx = 14
        up_move = high.diff()           # signed: high - prev_high
        down_move = -low.diff()         # signed: prev_low - low  (NOT abs())

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

        plus_dm_series = pd.Series(plus_dm, index=df.index)
        minus_dm_series = pd.Series(minus_dm, index=df.index)

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        def wilders_smoothing(series, periods):
            return series.ewm(alpha=1/periods, adjust=False).mean()

        atr = wilders_smoothing(tr, n_adx)
        plus_di = 100 * (wilders_smoothing(plus_dm_series, n_adx) / atr)
        minus_di = 100 * (wilders_smoothing(minus_dm_series, n_adx) / atr)

        dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
        adx_series = wilders_smoothing(dx, n_adx)
        current_adx = adx_series.iloc[-1]

        # --- Directional context (+DI vs -DI) ---
        current_plus_di = plus_di.iloc[-1]
        current_minus_di = minus_di.iloc[-1]
        bullish_direction = current_plus_di > current_minus_di

        # --- Multi-Factor Synthesis ---
        # Tiered classification that handles indicator disagreement
        # (e.g., ADX says trend, ER says chop).
        is_above_200 = current_price > sma_200
        adx_trending = current_adx > 25      # Conventional ADX trend threshold
        er_trending = current_ema_er >= 0.20  # ER above 0.20 = directional path
        adx_strong = current_adx > 50         # Very high directional strength;
                                              # threshold chosen to isolate the
                                              # "violent trend with whipsaws"
                                              # regime from ordinary trends.

        if not is_above_200:
            # Below 200 SMA — qualify the move with direction AND strength
            if adx_trending:
                if bullish_direction:
                    # Counter-trend rally inside a bear regime
                    status = "Bear (Counter-Trend Rally)"
                    desc = (f"Below 200 SMA but +DI > -DI with strong ADX — "
                            f"counter-trend bounce in effect. "
                            f"ADX: {current_adx:.1f}, ER EMA: {current_ema_er:.2f}, "
                            f"+DI: {current_plus_di:.1f}, -DI: {current_minus_di:.1f}.")
                else:
                    status = "Bear (Trending Down)"
                    desc = (f"Below 200 SMA with strong directional movement to the downside. "
                            f"ADX: {current_adx:.1f}, ER EMA: {current_ema_er:.2f}, "
                            f"+DI: {current_plus_di:.1f}, -DI: {current_minus_di:.1f}.")
            else:
                status = "Bear (Weak/Drifting)"
                desc = (f"Below 200 SMA but lacking directional conviction. "
                        f"ADX: {current_adx:.1f}, ER EMA: {current_ema_er:.2f}.")
        elif adx_trending and er_trending:
            # Both agree: clean trend
            status = "Bull (Trend)"
            desc = (f"Above 200 SMA with confirmed trend structure. "
                    f"ADX: {current_adx:.1f}, ER EMA: {current_ema_er:.2f}.")
        elif adx_strong and not er_trending:
            # ADX very high but ER low: volatile directional move with whipsaws.
            # This is the ADX=82 / ER=0.20 scenario — NOT sideways.
            direction = "bullish" if bullish_direction else "bearish"
            status = f"Volatile Trend ({direction.title()})"
            desc = (f"Above 200 SMA. ADX ({current_adx:.1f}) signals strong directional "
                    f"movement but ER EMA ({current_ema_er:.2f}) shows path inefficiency — "
                    f"expect continuation with sharp reversals. +DI: {current_plus_di:.1f}, -DI: {current_minus_di:.1f}.")
        elif adx_trending and not er_trending:
            # Moderate ADX with low ER: direction emerging but not yet clean
            status = "Transitional"
            desc = (f"Above 200 SMA. ADX ({current_adx:.1f}) shows building direction "
                    f"but ER EMA ({current_ema_er:.2f}) hasn't confirmed. Watch for breakout or failure.")
        else:
            # Neither ADX nor ER showing trend — genuine chop
            status = "Sideways (Chop)"
            desc = (f"Above 200 SMA, but lacking directional momentum. "
                    f"ADX: {current_adx:.1f}, ER EMA: {current_ema_er:.2f}.")

        return {
            'status': status,
            'metric_label': f"Price: ${current_price:.2f}",
            'er': f"{current_er:.2f} (EMA: {current_ema_er:.2f})",
            'adx': f"{current_adx:.1f}",
            'di': f"+DI: {current_plus_di:.1f} / -DI: {current_minus_di:.1f}",
            'details': desc
        }
    except Exception as e:
        print(f"Regime calc failed: {e}")
        return None


# ============================================================
# BROKER CHECKS — Untouched, still runs on ALL tickers
# ============================================================
def load_local_tickers():
    """Load tickers from active_tickers.txt.

    Supports comma-separated tickers across one or more lines.
    Lines beginning with '#' are treated as comments and ignored.
    Blank lines are ignored. Tokens starting with '$' are skipped
    (Composer cash placeholders like $USD). '/' is normalized to '.'
    so 'BRK/B' becomes 'BRK.B' for broker compatibility.
    """
    try:
        with open(FILE_PATH, "r") as f:
            # Drop comment lines and blank lines, then re-join
            content_lines = []
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                content_lines.append(line)
            content = ",".join(content_lines)
            if not content:
                return []
            raw_tickers = [t.strip() for t in content.split(",")]
            clean_tickers = []
            for t in raw_tickers:
                if not t or t.startswith("$"):
                    continue
                # Normalize: '/' -> '.' (BRK/B -> BRK.B) and uppercase
                # so user-typed lowercase entries (tqqq) still match the
                # Alpaca asset map, which is keyed on uppercase symbols.
                clean_tickers.append(t.replace("/", ".").upper())
            return clean_tickers
    except FileNotFoundError:
        print(f"File not found: {FILE_PATH}")
        return []


def _fetch_alpaca_assets():
    """Fetch full active asset list from Alpaca. Returns symbol→asset dict, or None on failure."""
    try:
        res = requests.get(f"{BASE_URL}/assets?status=active", headers=HEADERS, timeout=15)
        if not res.ok:
            print(f"Alpaca asset fetch returned HTTP {res.status_code}")
            return None
        assets = res.json()
        return {a['symbol']: a for a in assets}
    except Exception as e:
        print(f"Alpaca asset fetch failed: {e}")
        return None


def check_asset_tradability(my_tickers, asset_map):
    """Flag tickers missing from Alpaca's active list or marked untradable.
    Expects pre-fetched asset_map from _fetch_alpaca_assets()."""
    print("Checking live asset tradability...")
    warnings = []
    if not asset_map:
        return warnings
    for t in my_tickers:
        if t not in asset_map:
            warnings.append(f"❓ {t}: Not found in active broker database.")
        elif not asset_map[t].get('tradable', True):
            warnings.append(f"🛑 {t}: Marked as UNTRADABLE by broker.")
    return warnings


def check_corporate_actions(my_tickers):
    print("Scanning forward-looking corporate actions calendar...")
    warnings = []
    today = datetime.now(timezone.utc).date()
    two_weeks_out = today + timedelta(days=14)

    params = {
        "ca_types": "merger,spinoff",
        "since": today.strftime("%Y-%m-%d"),
        "until": two_weeks_out.strftime("%Y-%m-%d")
    }
    try:
        res = requests.get(f"{BASE_URL}/corporate_actions/announcements", headers=HEADERS, params=params, timeout=15)
        if not res.ok:
            return []
        events = res.json()
        for event in events:
            target_sym = event.get('target_symbol')
            if target_sym in my_tickers:
                ca_type = event.get('ca_type', 'unknown event').replace("_", " ").title()
                ex_date = event.get('ex_date', 'Unknown Date')
                warnings.append(f"⚠️ {target_sym}: {ca_type} scheduled for {ex_date}")
    except Exception as e:
        print(f"Corporate actions check failed: {e}")
    return warnings


# ============================================================
# ADVANCE-NOTICE WARNING — Layer 1 (SEC EDGAR full-text search)
# ============================================================
def check_edgar_filings(my_tickers):
    """
    Query SEC EDGAR full-text search for recent filings (past N days)
    mentioning each ticker AND any closure/reorganization keyword.

    EDGAR is the primary advance-notice signal: ETF issuers file 497
    prospectus supplements when announcing fund closures, typically
    30-60 days before the actual liquidation date. Same-day filings
    cover symbol/name changes and reorganizations.

    Returns a list of warning strings, deduplicated by accession number.
    """
    print(f"Scanning SEC EDGAR for advance-notice filings ({EDGAR_LOOKBACK_DAYS}d lookback)...")
    warnings = []
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=EDGAR_LOOKBACK_DAYS)
    seen_accessions = set()
    total_queried = 0
    total_failed = 0

    for ticker in my_tickers:
        # Quoted ticker = exact-phrase match. Critical for short tickers
        # (V, GE, USD, TIP, etc.) which would otherwise match anything.
        query = f'"{ticker}" AND ({EDGAR_KEYWORDS})'
        params = {
            "q": query,
            "forms": EDGAR_FORMS,
            "dateRange": "custom",
            "startdt": start_date.strftime("%Y-%m-%d"),
            "enddt": today.strftime("%Y-%m-%d"),
        }
        total_queried += 1
        query_succeeded = False
        try:
            res = requests.get(EDGAR_BASE_URL, params=params, headers=EDGAR_HEADERS, timeout=15)
            # Retry once on transient errors (429 rate-limit, 5xx backend hiccups).
            # SEC's efts.sec.gov returns transient 500s under load; single retry
            # typically resolves them.
            if res.status_code in EDGAR_RETRY_STATUS_CODES:
                time.sleep(EDGAR_RETRY_BACKOFF_SECONDS)
                res = requests.get(EDGAR_BASE_URL, params=params, headers=EDGAR_HEADERS, timeout=15)
            if not res.ok:
                print(f"  EDGAR query for {ticker} returned HTTP {res.status_code} (after retry)")
                total_failed += 1
                time.sleep(EDGAR_REQUEST_DELAY)
                continue

            query_succeeded = True
            data = res.json()
            hits = data.get("hits", {}).get("hits", []) or []
            ticker_hit_count = 0
            for hit in hits:
                if ticker_hit_count >= EDGAR_MAX_HITS_PER_TICKER:
                    break
                src = hit.get("_source", {}) or {}
                accession = src.get("adsh", "") or hit.get("_id", "")
                if accession in seen_accessions:
                    continue
                seen_accessions.add(accession)
                ticker_hit_count += 1

                form = src.get("form", "?")
                file_date = src.get("file_date", "?")
                display_names = src.get("display_names") or []
                issuer = display_names[0] if display_names else "Unknown filer"
                ciks = src.get("ciks", []) or []
                cik = ciks[0] if ciks else ""

                # Build a link to the issuer's recent filings of this form type
                if cik:
                    url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                           f"&CIK={cik}&type={form}&dateb=&owner=include&count=10")
                else:
                    url = ""

                warnings.append(
                    f"📄 {ticker}: {form} filed {file_date} by {issuer} "
                    f"matches closure/reorganization keywords. {url}"
                )
        except Exception as e:
            print(f"  EDGAR query failed for {ticker}: {str(e)[:120]}")
            if not query_succeeded:
                total_failed += 1

        time.sleep(EDGAR_REQUEST_DELAY)

    # Scan-health summary
    succeeded = total_queried - total_failed
    failure_rate = (total_failed / total_queried) if total_queried else 0.0
    print(f"  EDGAR scan complete: {total_queried} queried, {succeeded} succeeded, "
          f"{total_failed} failed ({failure_rate:.1%}). "
          f"{len(warnings)} unique filing(s) flagged.")

    # Surface degraded coverage as a warning at the TOP of Section 5 so the user
    # knows when advance-notice scanning is unreliable for the day.
    if total_queried and failure_rate > EDGAR_DEGRADED_COVERAGE_THRESHOLD:
        warnings.insert(0,
            f"⚠️ EDGAR SCAN HEALTH: {total_failed}/{total_queried} queries failed "
            f"({failure_rate:.1%}) — advance-notice coverage degraded today. "
            f"Re-run later or check SEC status."
        )

    return warnings


# ============================================================
# ADVANCE-NOTICE WARNING — Layer 2 (Alpaca asset-list diff)
# ============================================================
def check_alpaca_asset_diff(my_tickers, current_assets_map):
    """
    Diff today's Alpaca asset state for our tickers against the persisted
    snapshot from the previous run. Detects:
      - Tickers that disappeared from the active list (delisting fired)
      - tradable flag flipped from True to False (broker restriction)
      - Asset name changed (CUSIP-preserving rebrand indicator)

    This is a same-day backup to the EDGAR scanner. If EDGAR missed an
    announcement, this catches the change the morning after Alpaca's
    asset database reflects it.

    Always persists today's state for tomorrow's diff.
    """
    print("Diffing Alpaca asset list against previous snapshot...")
    warnings = []

    # Load previous snapshot (first run = no prior baseline)
    prev_state = {}
    try:
        with open(PREVIOUS_ASSETS_FILE, "r") as f:
            prev_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("  No previous asset snapshot found. Establishing baseline.")

    # Build today's state for our tickers only (keeps file small + relevant)
    today_state = {}
    for t in my_tickers:
        a = current_assets_map.get(t)
        if a is None:
            today_state[t] = {"present": False}
        else:
            today_state[t] = {
                "present": True,
                "tradable": bool(a.get("tradable", True)),
                "name": a.get("name", "") or "",
                "status": a.get("status", "") or "",
            }

    # Diff against prior baseline (skip on first run when prev_state is empty)
    if prev_state:
        for t in my_tickers:
            prev = prev_state.get(t)
            curr = today_state[t]
            if prev is None:
                continue  # ticker added to roster after last snapshot — no prior data

            # Disappearance — strongest signal that something just delisted
            if prev.get("present") and not curr.get("present"):
                warnings.append(
                    f"🔄 {t}: REMOVED from Alpaca active asset list since last run."
                )
                continue

            # Tradable flag flipped from True to False
            if (prev.get("present") and curr.get("present")
                    and prev.get("tradable") and not curr.get("tradable")):
                warnings.append(
                    f"🛑 {t}: Tradable flag flipped to FALSE since last run."
                )

            # Name changed (catches CUSIP-preserving rebrands)
            prev_name = prev.get("name", "") or ""
            curr_name = curr.get("name", "") or ""
            if prev_name and curr_name and prev_name != curr_name:
                warnings.append(
                    f'📝 {t}: Name changed: "{prev_name}" → "{curr_name}"'
                )

    # Persist today's state for tomorrow's diff
    try:
        with open(PREVIOUS_ASSETS_FILE, "w") as f:
            json.dump(today_state, f, indent=2)
    except Exception as e:
        print(f"  Failed to persist asset snapshot: {e}")

    return warnings


# ============================================================
# AI ANALYSIS — Exception-only, fed with numbers not headlines
# ============================================================
def get_ai_analysis(internals, regime_data, triggers):
    """
    Called ONLY when exception triggers fire.
    Feeds Gemini hard numbers and asks for interpretation — not headline regurgitation.
    """
    print(f"Exception triggers fired ({len(triggers)}). Calling Gemini for analysis...")

    # Build a concise data payload for the LLM
    data_block = f"""
MARKET INTERNALS SNAPSHOT:
- VIX: {internals.get('vix', 'N/A')} (prev: {internals.get('vix_prev', 'N/A')}, change: {internals.get('vix_change_pct', 'N/A')}%)
- Yield Curve (10Y-2Y): {internals.get('yield_spread', 'N/A')} (10Y: {internals.get('yield_10y', 'N/A')}, 2Y: {internals.get('yield_2y', 'N/A')})
- SPY: ${internals.get('spy_price', 'N/A')} ({internals.get('spy_change_pct', 'N/A')}% daily)

TECHNICAL REGIME (SPY):
- Status: {regime_data.get('status', 'N/A') if regime_data else 'N/A'}
- ADX: {regime_data.get('adx', 'N/A') if regime_data else 'N/A'}
- Efficiency Ratio: {regime_data.get('er', 'N/A') if regime_data else 'N/A'}

EXCEPTION TRIGGERS THAT FIRED:
{chr(10).join(f'- {t}' for t in triggers)}
"""

    prompt = f"""You are the Chief Risk Officer of an algorithmic trading portfolio spanning equities, bonds, commodities, and inverse ETFs.

The following market internals have triggered exception alerts. Analyze the DATA ONLY — do not speculate beyond what the numbers show.

{data_block}

Respond with a strict JSON object:
1. "risk_score": Integer from -10 to +10. Based strictly on the data above:
   0 = Neutral. +3 to +5 = Favorable conditions. -3 to -5 = Elevated stress. -8 to -10 = Crisis-level readings.
2. "assessment": 2-3 sentences. What do these specific readings tell us about current market structure? Be concrete — reference the actual numbers.
3. "action_items": List of 1-3 strings. Specific, actionable considerations for portfolio risk management based on these readings. No generic advice.
"""

    # Retry with fallback
    models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash']
    max_retries = 3
    last_error = None

    for model_name in models_to_try:
        for attempt in range(1, max_retries + 1):
            try:
                print(f"  Attempt {attempt}/{max_retries} with {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={
                        'response_mime_type': 'application/json',
                        'temperature': 0.0
                    }
                )
                data = json.loads(response.text)

                if model_name != models_to_try[0]:
                    data['_model_used'] = model_name

                # Enrich payload so any downstream consumer of daily_intel.json
                # has the full context, not just the AI response.
                data['internals'] = internals
                data['triggers'] = triggers
                data['status'] = 'exception'
                data['date'] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                # Save to disk for GitHub Action commit
                with open("daily_intel.json", "w") as f:
                    json.dump(data, f, indent=4)

                print(f"  Success with {model_name} on attempt {attempt}.")
                return data

            except Exception as e:
                last_error = e
                error_str = str(e)
                if any(code in error_str for code in ['503', '429', 'UNAVAILABLE', 'timeout', 'Timeout']):
                    wait_time = 2 ** attempt * 5  # 10s, 20s, 40s
                    print(f"  Transient error ({error_str[:80]}). Waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"  Non-transient error: {error_str[:120]}. Skipping retries for {model_name}.")
                    break

        print(f"  Exhausted retries for {model_name}. Trying next model...")

    print(f"All models and retries exhausted. Last error: {last_error}")
    return {
        "risk_score": 0,
        "assessment": f"AI analysis unavailable (all retries failed): {last_error}",
        "action_items": [],
        "internals": internals,
        "triggers": triggers,
        "status": "exception",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# ============================================================
# EMAIL FORMATTING
# ============================================================
def send_email_alert(warnings, internals, regime_data, triggers, ai_data=None,
                     advance_warnings=None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return
    advance_warnings = advance_warnings or []

    # --- Section 0: Technical Regime ---
    if regime_data:
        adx_text = f"\nADX: {regime_data['adx']}" if 'adx' in regime_data else ""
        di_text = f"\n{regime_data['di']}" if 'di' in regime_data else ""
        regime_text = (
            f"Status: {regime_data['status']}\n"
            f"{regime_data['metric_label']}\n"
            f"ER: {regime_data['er']}{adx_text}{di_text}\n"
            f"Details: {regime_data['details']}"
        )
    else:
        regime_text = "Technical regime data unavailable."

    # --- Section 1: Market Internals Dashboard ---
    vix_str = f"{internals.get('vix', 'N/A')}"
    if internals.get('vix_change_pct') is not None:
        vix_str += f" ({internals['vix_change_pct']:+.1f}% daily)"

    yield_str = f"{internals.get('yield_spread', 'N/A')}"
    if internals.get('yield_10y') is not None:
        yield_str += f" (10Y: {internals['yield_10y']}, 2Y: {internals['yield_2y']})"

    spy_str = f"${internals.get('spy_price', 'N/A')}"
    if internals.get('spy_change_pct') is not None:
        spy_str += f" ({internals['spy_change_pct']:+.2f}%)"

    internals_text = (
        f"VIX: {vix_str}\n"
        f"Yield Curve (10Y-2Y): {yield_str}\n"
        f"SPY: {spy_str}"
    )

    # --- Section 2: Exception Triggers ---
    if triggers:
        triggers_text = "\n".join([f"🚨 {t}" for t in triggers])
    else:
        triggers_text = "✅ All market internals within normal bands."

    # --- Section 3: AI Risk Assessment (only if triggered) ---
    if ai_data:
        score = ai_data.get('risk_score', 0)
        assessment = ai_data.get('assessment', 'N/A')
        actions = ai_data.get('action_items', [])
        actions_text = "\n".join([f"  → {a}" for a in actions]) if actions else "  No specific actions recommended."
        model_note = f"\n(Fallback model: {ai_data['_model_used']})" if '_model_used' in ai_data else ""

        ai_text = (
            f"Risk Score: {score} / 10\n"
            f"Assessment: {assessment}\n"
            f"Action Items:\n{actions_text}{model_note}"
        )
    else:
        ai_text = "No exceptions triggered — AI analysis skipped."

    # --- Section 4: Broker Status ---
    warning_text = "\n".join(warnings) if warnings else "✅ No Alpaca broker warnings."

    # --- Section 5: Advance-Notice Warning (EDGAR + Alpaca asset diff) ---
    if advance_warnings:
        advance_text = "\n".join(advance_warnings)
    else:
        advance_text = "✅ No EDGAR matches and no Alpaca asset-list changes since last run."

    # --- Compose ---
    if advance_warnings:
        subject_prefix = "📄 TICKER ADVANCE WARNING"
    elif ai_data and ai_data.get('risk_score', 0) <= -5:
        subject_prefix = "⚠️ ELEVATED RISK"
    elif triggers:
        subject_prefix = "🔔 Exception Alert"
    else:
        subject_prefix = "✅ Systems Normal"

    body = f"""COMPOSER TICKER CANARY — DAILY BRIEFING

--- 0. TECHNICAL REGIME (SPY) ---
{regime_text}

--- 1. MARKET INTERNALS ---
{internals_text}

--- 2. EXCEPTION TRIGGERS ---
{triggers_text}

--- 3. AI RISK ASSESSMENT ---
{ai_text}

--- 4. BROKER STATUS (ALPACA) ---
{warning_text}

--- 5. TICKER ADVANCE WARNING (SEC EDGAR + ALPACA DIFF) ---
{advance_text}
"""

    msg = MIMEMultipart()
    msg['From'] = GMAIL_ADDRESS
    msg['To'] = GMAIL_ADDRESS
    msg['Subject'] = f"Canary Briefing — {subject_prefix}"
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email alert dispatched.")
    except Exception as e:
        print(f"Failed to send email alert: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    print("Starting Composer Ticker Canary (GitHub Actions)...")

    # --- Load all tickers (full list for broker checks) ---
    local_tickers = load_local_tickers()
    macro_tickers = ["SPY", "QQQ", "GLD", "TLT"]
    my_tickers = list(set(local_tickers + macro_tickers))

    if not my_tickers:
        return

    # --- Fetch Alpaca asset list once, share across checks ---
    asset_map = _fetch_alpaca_assets() or {}

    # --- Broker checks on ALL tickers ---
    broker_warnings = []
    broker_warnings.extend(check_asset_tradability(my_tickers, asset_map))
    broker_warnings.extend(check_corporate_actions(my_tickers))

    # --- Advance-notice warnings: same-day Alpaca diff + EDGAR scan ---
    advance_warnings = []
    advance_warnings.extend(check_alpaca_asset_diff(my_tickers, asset_map))
    advance_warnings.extend(check_edgar_filings(my_tickers))

    # --- Quantitative data (no AI, no external API besides yfinance) ---
    regime_data = calculate_market_regime("SPY")
    internals = fetch_market_internals()

    # --- Exception trigger evaluation ---
    triggers = check_exception_triggers(internals, regime_data)

    # --- AI analysis: ONLY if exceptions fired ---
    ai_data = None
    if triggers:
        ai_data = get_ai_analysis(internals, regime_data, triggers)
    else:
        print("✅ All internals within normal bands. Skipping Gemini call.")
        # Write a daily intel record on every run, including quiet days, so the
        # JSON is always present and consistent for any downstream consumer.
        with open("daily_intel.json", "w") as f:
            json.dump({
                "status": "normal",
                "triggers": [],
                "internals": internals,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }, f, indent=4)

    # --- Persist regime for tomorrow's change detection ---
    _save_current_regime(regime_data)

    # --- Always send the briefing (but AI section is empty on quiet days) ---
    send_email_alert(broker_warnings, internals, regime_data, triggers, ai_data,
                     advance_warnings=advance_warnings)
    print("Briefing complete.")


if __name__ == "__main__":
    main()
