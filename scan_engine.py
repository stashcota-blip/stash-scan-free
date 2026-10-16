"""STASH SCAN PRO v2.1 - educational multi-timeframe radar, NEVER an MT5 execution feed."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
import math
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger(__name__)
UTC = timezone.utc
SI = ZoneInfo('Europe/Ljubljana')
SYMBOLS = {'GOLD_FUTURES_PROXY': 'GC=F', 'NASDAQ_FUTURES_PROXY': 'NQ=F'}
PRICE_NOTE = 'Futures proxy (GC=F/NQ=F), NI cena XAUUSD/NDX100 na MT5.'


def client() -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; StashScanPro/2.1; personal research)'})
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.7,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


def yahoo_1m(ticker: str, session: requests.Session | None = None, now: datetime | None = None) -> pd.DataFrame:
    """Yahoo unofficial public chart endpoint, subject to delay/rate limits."""
    now = now or datetime.now(UTC)
    sess = session or client()
    last_err = None
    for host in ('query1.finance.yahoo.com', 'query2.finance.yahoo.com'):
        try:
            url = f'https://{host}/v8/finance/chart/{quote(ticker, safe="")}'
            r = sess.get(url, params={'interval': '1m', 'range': '7d', 'events': 'false'}, timeout=15)
            r.raise_for_status()
            body = r.json().get('chart', {})
            if body.get('error'):
                raise ValueError(f'Yahoo error: {body["error"]}')
            res = (body.get('result') or [None])[0]
            if not res:
                raise ValueError('Missing chart')
            stamps = res.get('timestamp') or []
            q = res['indicators']['quote'][0]
            if not stamps:
                raise ValueError('Missing timestamps')
            df = pd.DataFrame({'open': q['open'], 'high': q['high'],
                               'low': q['low'], 'close': q['close']},
                              index=pd.to_datetime(stamps, unit='s', utc=True))
            df = df.apply(pd.to_numeric, errors='coerce').dropna()
            df = df.loc[~df.index.duplicated(keep='last')].sort_index()
            # Reject the still-forming one-minute candle, never use future candles.
            df = df[df.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
            if len(df) < 300:
                raise ValueError(f'Only {len(df)} full M1 candles')
            return df
        except Exception as e:
            last_err = e
            LOG.warning('%s source error: %s', host, e)
    raise RuntimeError(f'Data unavailable for {ticker}: {last_err}')


def resample_closed(df: pd.DataFrame, freq: str, now: datetime) -> pd.DataFrame:
    """Only closed, reasonably complete buckets; no incomplete candle masquerades as M15/H1.

    Yahoo may publish bars with a lag. A bucket is NOT complete merely because the
    wall-clock passed its end: it must also contain recent source observations.
    """
    frame = df.sort_index()
    grouped = frame.resample(freq, label='left', closed='left')
    bars = grouped.agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    mins = pd.Timedelta(freq) / pd.Timedelta(minutes=1)
    counts = grouped['close'].count().reindex(bars.index).fillna(0)
    latest_observation = frame.index.to_series().resample(freq, label='left', closed='left').max()
    latest_observation = latest_observation.reindex(bars.index)
    end = bars.index + pd.Timedelta(freq)
    covered = counts >= max(1, int(math.ceil(mins * 0.80)))
    tail_complete = (end - pd.Timedelta(minutes=1) - latest_observation) <= pd.Timedelta(minutes=2)
    finished = end <= pd.Timestamp(now)
    return bars[covered & tail_complete & finished]


def atr(df: pd.DataFrame, length=14) -> pd.Series:
    prev = df.close.shift(1)
    true_range = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def ema_bias(df: pd.DataFrame) -> str:
    if len(df) < 58:
        return 'NEUTRAL'
    e20 = df.close.ewm(span=20, adjust=False).mean()
    e50 = df.close.ewm(span=50, adjust=False).mean()
    rising = e20.iloc[-1] > e20.iloc[-4] and e50.iloc[-1] > e50.iloc[-4]
    falling = e20.iloc[-1] < e20.iloc[-4] and e50.iloc[-1] < e50.iloc[-4]
    if df.close.iloc[-1] > e20.iloc[-1] > e50.iloc[-1] and rising:
        return 'BUY'
    if df.close.iloc[-1] < e20.iloc[-1] < e50.iloc[-1] and falling:
        return 'SELL'
    return 'NEUTRAL'


@dataclass(frozen=True)
class Signal:
    name: str
    ticker: str
    direction: str
    candle_at: datetime
    sweep_at: datetime
    entry: float
    stop: float
    tp1: float
    tp2: float
    score: int
    bias_h1: str
    bias_m30: str
    reason: str
    source_age_min: float
    news_status: str = ''

    @property
    def key(self) -> str:
        return f'{self.ticker}:{self.direction}:{self.candle_at.isoformat()}:{self.sweep_at.isoformat()}'


def find_sweep(m15: pd.DataFrame, direction: str, num=12, freshness=6):
    """M15 sweep and close back inside the previous 12 bars, not lookahead."""
    if len(m15) < num + freshness + 2:
        return None
    a = atr(m15).fillna(0)
    for i in range(len(m15) - 1, max(num, len(m15) - freshness - 1), -1):
        candle = m15.iloc[i]
        previous = m15.iloc[i - num:i]
        pad = max(0.02 * float(a.iloc[i]), 0.000001)
        if direction == 'BUY':
            lev = float(previous.low.min())
            valid = candle.low < lev - pad and candle.close > lev and candle.close > candle.open
        else:
            lev = float(previous.high.max())
            valid = candle.high > lev + pad and candle.close < lev and candle.close < candle.open
        if valid:
            return m15.index[i], float(candle.low if direction == 'BUY' else candle.high), lev
    return None


def find_bos(m5: pd.DataFrame, direction: str, after: datetime, lookback=5):
    """First M5 close breaking pre-existing 5-bar high/low AFTER M15 sweep closes."""
    if len(m5) < lookback + 2:
        return None
    for i in range(lookback, len(m5)):
        ts = m5.index[i]
        if ts < pd.Timestamp(after) + pd.Timedelta(minutes=15):
            continue
        if ts > pd.Timestamp(after) + pd.Timedelta(minutes=50):
            break
        previous = m5.iloc[i - lookback:i]
        boundary = float(previous.high.max() if direction == 'BUY' else previous.low.min())
        c = m5.iloc[i]
        if direction == 'BUY' and c.close > boundary and c.close > c.open:
            return ts, boundary
        if direction == 'SELL' and c.close < boundary and c.close < c.open:
            return ts, boundary
    return None


def detect_signal(name: str, ticker: str, m1: pd.DataFrame, now: datetime) -> tuple[Optional[Signal], str]:
    """Conditional research setup; must have near-current candles for a time-sensitive alert."""
    if m1.empty or len(m1) < 400:
        return None, 'premalo sveč'
    age = (pd.Timestamp(now) - (m1.index[-1] + pd.Timedelta(minutes=1))).total_seconds() / 60
    if age < -0.1:
        return None, 'nepravilna ura podatkov'
    if age > 2.5:
        return None, f'zastareli podatki ({age:.1f} min) — M1 SIGNAL BLOKIRAN'
    if m1.index[-1] - m1.index[-20] > pd.Timedelta(minutes=25):
        return None, 'luknje v M1 podatkih — signal blokiran'
    m5 = resample_closed(m1, '5min', now)
    m15 = resample_closed(m1, '15min', now)
    m30 = resample_closed(m1, '30min', now)
    h1 = resample_closed(m1, '1h', now)
    if min(len(m5), len(m15), len(m30), len(h1)) < 58:
        return None, 'premalo popolnih sveč za vse timeframe'
    h1_dir, m30_dir = ema_bias(h1), ema_bias(m30)
    if h1_dir == 'NEUTRAL' or h1_dir != m30_dir:
        return None, f'ni soglasja H1={h1_dir}, M30={m30_dir}'
    d = h1_dir
    structure = swing_structure(h1)
    if (d == 'BUY' and structure == 'LH/LL') or (d == 'SELL' and structure == 'HH/HL'):
        return None, f'H1 swing struktura {structure} nasprotuje EMA trendu'
    m15_bias = ema_bias(m15)
    if m15_bias != 'NEUTRAL' and m15_bias != d:
        return None, f'M15 bias {m15_bias} nasprotuje H1 {d}'
    sw = find_sweep(m15, d)
    if sw is None:
        return None, f'ni svežega M15 liquidity sweep ({d})'
    sweep_ts, extreme, _ = sw
    bos = find_bos(m5, d, sweep_ts)
    if bos is None:
        return None, 'ni potrjenega M5 break of structure'
    bos_ts, level = bos
    candle = m1.iloc[-1]
    stamp = m1.index[-1]
    if stamp < bos_ts + pd.Timedelta(minutes=5):
        return None, 'čakamo M1 retest za BOS'
    if stamp > bos_ts + pd.Timedelta(minutes=25):
        return None, 'M5 BOS je prestar'
    a = float(atr(m5).iloc[-1])
    if not math.isfinite(a) or a <= 0:
        return None, 'ATR ni na voljo'
    pad = max(a * 0.2, 0.01)
    if d == 'BUY':
        confirmed = candle.low <= level + pad and candle.close > level and candle.close > candle.open
    else:
        confirmed = candle.high >= level - pad and candle.close < level and candle.close < candle.open
    if not confirmed:
        return None, 'čakamo potrjen M1 retest'
    entry = float(candle.close)
    recent = m5[m5.index >= pd.Timestamp(bos_ts) - pd.Timedelta(minutes=15)]
    if d == 'BUY':
        stop = min(float(recent.low.min()), extreme) - 0.1 * a
        risk = entry - stop
        nearest = float(m15.high.tail(16).max())
        if nearest <= entry + 2.0 * risk:
            return None, 'premalo prostora do M15 upora za TP2'
        tp1, tp2 = entry + 1.5 * risk, entry + 2.0 * risk
    else:
        stop = max(float(recent.high.max()), extreme) + 0.1 * a
        risk = stop - entry
        nearest = float(m15.low.tail(16).min())
        if nearest >= entry - 2.0 * risk:
            return None, 'premalo prostora do M15 podpore za TP2'
        tp1, tp2 = entry - 1.5 * risk, entry - 2.0 * risk
    if risk <= 0 or risk > 2.5 * a or risk < 0.12 * a:
        return None, f'neprimerna razdalja SL ({risk:.2f})'
    score = 4 + int(structure in ('HH/HL' if d == 'BUY' else 'LH/LL',))
    return Signal(name, ticker, d, stamp.to_pydatetime(), sweep_ts.to_pydatetime(),
                  entry, stop, tp1, tp2, score, h1_dir, m30_dir,
                  'H1/M30 trend + M15 sweep + M5 BOS + M1 retest', round(age, 1)), 'setup'


def get_usd_news(now: datetime, session: requests.Session) -> tuple[bool, str]:
    """Fail closed: an unavailable unofficial calendar must not be called news-safe."""
    try:
        r = session.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=10)
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list) or len(events) == 0:
            raise ValueError('empty calendar')
        high_events = []
        for item in events:
            if item.get('country') != 'USD' or str(item.get('impact', '')).lower() != 'high':
                continue
            when = datetime.fromisoformat(str(item.get('date', '')).replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError('calendar event without timezone')
            high_events.append((when.astimezone(UTC), str(item.get('title', 'USD news'))))
        if not high_events:
            raise ValueError('no high-impact USD events in calendar; uncertain coverage')
        closest = sorted(high_events, key=lambda a: abs((a[0] - now).total_seconds()))[0]
        dt = (closest[0] - now).total_seconds()/60
        if -45 <= dt <= 30:
            return True, f'🚨 HIGH USD: {closest[1]} ({closest[0].astimezone(SI):%d.%m. %H:%M} SI, {dt:+.0f} min)'
        forthcoming = sorted((e for e in high_events if e[0] > now), key=lambda a: a[0])
        if forthcoming:
            nxt = forthcoming[0]
            return False, f'Koledar OK; naslednja High USD: {nxt[1]} ({nxt[0].astimezone(SI):%d.%m. %H:%M} SI)'
        return False, 'Koledar OK; danes ni nadaljnjih navedenih High USD dogodkov'
    except Exception as exc:
        LOG.warning('News calendar unavailable: %s', type(exc).__name__)
        return True, 'KOLEDAR NEPREVERJEN — brez novih alertov; preveri novice ročno'


def format_signal(s: Signal, news: str, allowed: bool) -> str:
    time_si = s.candle_at.astimezone(SI).strftime('%d.%m. %H:%M')
    prefix = '🛑 BLOKIRAN TESTNI SETUP' if not allowed else '🔬 TESTNI SIGNAL (NE ZA IZVEDBO NA MT5)'
    fmt = lambda x: f'{x:,.2f}'
    return (f'{prefix}\n{s.name} / {s.ticker} {s.direction}\n'
            f'Vir: {PRICE_NOTE}\n'
            f'Sveča: {time_si} SI | vir zamuja ~{s.source_age_min:.0f} min\n'
            f'FUTURES PROXY Entry: {fmt(s.entry)}\n'
            f'FUTURES PROXY SL: {fmt(s.stop)}\n'
            f'FUTURES PROXY TP1: {fmt(s.tp1)} | TP2: {fmt(s.tp2)}\n'
            f'Model: {s.reason}\nH1={s.bias_h1}, M30={s.bias_m30}, kakovost {s.score}/5\n'
            f'Novice: {news}\n'
            '⚠️ Podatki so lahko zamaknjeni. To NISO cene tvojega brokerja. '
            'Ne kopiraj teh entry/SL/TP nivojev v MT5. Pred dejanskim trejdom je potreben live bid/ask in test strategije.')


def scan_status(name: str, ticker: str, df: pd.DataFrame, now: datetime, note: str) -> str:
    age = (pd.Timestamp(now) - df.index[-1] - pd.Timedelta(minutes=1)).total_seconds() / 60
    return f'{name} ({ticker}): {note}; zadnja zaprta M1 {df.index[-1].astimezone(SI):%H:%M} SI; zamik ~{age:.0f} min'


# ------------------ STASH SCAN PRO v2: multi-timeframe research radar ------------------

def swing_structure(frame: pd.DataFrame, left: int = 2, right: int = 2) -> str:
    """HH/HL or LH/LL from confirmed fractal swings; no future-candle lookahead."""
    if len(frame) < 20:
        return 'RANGE'
    f = frame.tail(160)
    highs, lows = [], []
    for i in range(left, len(f) - right):
        hi = float(f.high.iloc[i]); lo = float(f.low.iloc[i])
        if all(hi > float(x) for x in f.high.iloc[i-left:i]) and all(
                hi >= float(x) for x in f.high.iloc[i+1:i+right+1]):
            highs.append(hi)
        if all(lo < float(x) for x in f.low.iloc[i-left:i]) and all(
                lo <= float(x) for x in f.low.iloc[i+1:i+right+1]):
            lows.append(lo)
    if len(highs) < 2 or len(lows) < 2:
        return 'RANGE'
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 'HH/HL'
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return 'LH/LL'
    return 'RANGE'


def _last_complete_day_levels(df: pd.DataFrame, now: datetime):
    local_index = df.index.tz_convert(SI)
    day = now.astimezone(SI).date()
    previous = sorted({d for d in local_index.date if d < day})
    if not previous:
        return None
    prev = previous[-1]
    sub = df[local_index.date == prev]
    if len(sub) < 60:
        return None
    return float(sub.high.max()), float(sub.low.min()), str(prev)


def _today_session_levels(df: pd.DataFrame, now: datetime):
    idx = df.index.tz_convert(SI)
    today = now.astimezone(SI).date()
    # Simple Europe/Ljubljana clock-time segments (approximate sessions).
    out = {}
    for label, start, end in [('ASIA', 0, 8), ('LONDON', 8, 15), ('NEW_YORK', 15, 23)]:
        mask = (idx.date == today) & (idx.hour >= start) & (idx.hour < end)
        d = df[mask]
        if len(d) > 3:
            out[label] = (float(d.high.max()), float(d.low.min()))
    return out


def market_overview(name: str, ticker: str, m1: pd.DataFrame, now: datetime, reason: str) -> dict:
    """Top-down context on closed historical bars only; never broker bid/ask."""
    age = (pd.Timestamp(now) - (m1.index[-1] + pd.Timedelta(minutes=1))).total_seconds()/60
    tf = {label: resample_closed(m1, freq, now) for label, freq in [
        ('H4', '4h'), ('H1', '1h'), ('M30', '30min'), ('M15', '15min'), ('M5', '5min')]}
    bias = {k: ema_bias(v) for k, v in tf.items()}
    bias['M1'] = ema_bias(m1)
    # Use a shorter H4 EMA only as context when seven days of candles is insufficient for EMA50.
    if len(tf['H4']) >= 24:
        h4 = tf['H4']; e8 = h4.close.ewm(span=8, adjust=False).mean().iloc[-1]
        e20 = h4.close.ewm(span=20, adjust=False).mean().iloc[-1]
        bias['H4'] = ('BUY' if h4.close.iloc[-1] > e8 > e20 else
                       'SELL' if h4.close.iloc[-1] < e8 < e20 else 'NEUTRAL')
    structure = swing_structure(tf['H1'])
    m15_structure = swing_structure(tf['M15'])
    m5_structure = swing_structure(tf['M5'])
    pd_levels = _last_complete_day_levels(m1, now)
    sessions = _today_session_levels(m1, now)
    tr = atr(tf['M5']).iloc[-1] if len(tf['M5']) >= 15 else float('nan')
    atr_val = float(tr) if math.isfinite(float(tr)) else None
    m15 = tf['M15']; m5 = tf['M5']
    close = float(m1.close.iloc[-1])
    # Local structural levels are context, not direct MT5 entries.
    upper = float(m15.high.tail(16).max()) if len(m15) >= 2 else None
    lower = float(m15.low.tail(16).min()) if len(m15) >= 2 else None
    swing = None
    if len(m15) >= 18 and bias['H1'] == bias['M30'] and bias['H1'] in ('BUY', 'SELL'):
        swing = find_sweep(m15, bias['H1'])
    last_bos = None
    if swing is not None and len(m5) >= 8:
        last_bos = find_bos(m5, bias['H1'], swing[0])
    return {'name': name, 'ticker': ticker, 'close': close, 'age_min': max(0, age),
            'bar_at': m1.index[-1].to_pydatetime(), 'bias': bias, 'structure': structure,
            'structure_m15': m15_structure, 'structure_m5': m5_structure,
            'pdh_pdl': pd_levels, 'sessions': sessions, 'atr_m5': atr_val,
            'resistance': upper, 'support': lower,
            'sweep': swing, 'bos': last_bos,
            'reason': reason, 'data_fresh': 0 <= age <= 2.5}


def format_overview(info: dict) -> str:
    """Fast Telegram /scan output with visible missing confirmation and latency."""
    n = info
    b = n['bias']; age = n['age_min']
    is_fresh = n['data_fresh']
    out = [f"{'🟡' if not is_fresh else '🔎'} {n['name']} ({n['ticker']}) — NO TRADE / RAZISKOVALNI PREGLED",
           f"Vir: FUTURES PROXY, NE MT5 | zadnja M1 {n['bar_at'].astimezone(SI):%H:%M} SI | zamik {age:.1f} min",
           f"Cena proxy: {n['close']:,.2f} (NI brokerjeva cena)",
           f"H4={b['H4']} | H1={b['H1']} | M30={b['M30']} | M15={b['M15']} | M5={b['M5']} | M1={b['M1']}",
           f"Struktura H1={n['structure']} | M15={n['structure_m15']} | M5={n['structure_m5']}"]
    if n['pdh_pdl']:
        h,l,d=n['pdh_pdl']; out.append(f'PDH/PDL {d}: {h:,.2f} / {l:,.2f}')
    if n['sessions']:
        out.append('Seje High/Low: ' + ' | '.join(f'{k} {v[0]:,.2f}/{v[1]:,.2f}' for k,v in n['sessions'].items()))
    if n['support'] is not None:
        out.append(f'M15 high/low zadnjih 16 zaprtih sveč: {n["resistance"]:,.2f} / {n["support"]:,.2f}')
    if n['atr_m5'] is not None:
        out.append(f'M5 ATR14: {n["atr_m5"]:.2f} (volatilnost)')
    out.append('M15 sweep: ' + ('DA' if n['sweep'] else 'NE') + ' | M5 BOS: ' + ('DA' if n['bos'] else 'NE'))
    out.append('Odločitev: ' + n['reason'])
    if not is_fresh:
        out.append('⛔ M1 ENTRY/SL/TP BLOKIRANI: zastarel vir. Preveri svoj MT5 live graf.')
    else:
        out.append('⚠️ Tudi svež futures signal ni izvršljiv MT5 signal: cene/spread so različni.')
    return '\n'.join(out)


def format_watch(info: dict) -> str:
    b = info['bias']
    return ('👀 STRUKTURNI WATCH — NE VSTOPAJ NA SLEPO\n'
            f"{info['name']} ({info['ticker']}) | H1/M30 {b['H1']}\n"
            'M15 liquidity sweep zaznan na zamujenem futures viru.\n'
            f"Zadnja M1: {info['bar_at'].astimezone(SI):%H:%M} SI, zamik {info['age_min']:.1f} min.\n"
            'Potrditev M5 BOS, svež M1 retest in brokerjev bid/ask še zahtevani.\n'
            'To je WATCHLIST, NE BUY/SELL signal za FundedNext.')
