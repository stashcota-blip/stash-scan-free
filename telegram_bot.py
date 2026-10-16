"""STASH SCAN PRO v2.1 — five-minute research bot for Telegram (not trade execution).

Public repository safe: credentials must be GitHub Actions Secrets, never code.
One program invocation per scheduled GitHub Actions run; no always-on poller.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from scan_engine import (
    SYMBOLS, client, detect_signal, find_sweep, format_overview,
    format_signal, format_watch, get_usd_news, market_overview,
    resample_closed, yahoo_1m,
)

LOG = logging.getLogger('stash_scan_pro')
STATE_PATH = Path('bot_state.json')
SI = ZoneInfo('Europe/Ljubljana')
MAX_TRACKED = 75
VERSION = 'STASH SCAN PRO v2.1'


def state_load() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('invalid state')
    except (OSError, ValueError, TypeError):
        state = {}
    return {
        'last_update_id': int(state.get('last_update_id', 0)),
        'sent_keys': list(state.get('sent_keys', []))[-300:],
        'watch_keys': list(state.get('watch_keys', []))[-200:],
        'paper_trades': list(state.get('paper_trades', []))[-MAX_TRACKED:],
        'last_scan': state.get('last_scan', ''),
    }


def state_save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class Telegram:
    def __init__(self, token: str, session: requests.Session):
        self.url = f'https://api.telegram.org/bot{token}/'
        self.session = session

    def request(self, method: str, body: dict | None = None) -> dict:
        # Never allow an HTTP exception to print a URL containing the bot token.
        try:
            response = self.session.post(self.url + method, json=body or {}, timeout=20)
        except requests.RequestException:
            raise RuntimeError(f'Telegram {method}: network error (token withheld)') from None
        if not response.ok:
            raise RuntimeError(f'Telegram {method}: HTTP {response.status_code} (token withheld)')
        try:
            value = response.json()
        except ValueError:
            raise RuntimeError(f'Telegram {method}: invalid JSON (token withheld)') from None
        if not value.get('ok'):
            # Telegram messages might include arbitrary user data; don't log these.
            raise RuntimeError(f'Telegram {method}: API responded not OK (token withheld)')
        return value

    def say(self, chat: str | int, text: str) -> None:
        for i in range(0, len(text), 3700):
            self.request('sendMessage', {'chat_id': chat, 'text': text[i:i + 3700],
                                         'disable_web_page_preview': True})

    def updates(self, after: int) -> list:
        return self.request('getUpdates', {'offset': after, 'limit': 100,
                                           'timeout': 0, 'allowed_updates': ['message']}).get('result', [])


def paper_update(state: dict, ticker: str, df, now: datetime) -> None:
    """Non-executable proxy OHLC experiment. Stop-first if both levels cross same candle.

    No live fill/spread simulation. Always call it 'hypothetical', never a proven win rate.
    """
    for t in state['paper_trades']:
        if t.get('ticker') != ticker or t.get('outcome') != 'OPEN':
            continue
        try:
            candle_at = datetime.fromisoformat(t['candle_at'])
            later = df[df.index > candle_at]
            expiry = candle_at + timedelta(hours=24)
            later = later[later.index <= expiry]
            for _, c in later.iterrows():
                sl = float(t['stop']); tp = float(t['tp2'])
                if t['direction'] == 'BUY':
                    hit_sl, hit_tp = c.low <= sl, c.high >= tp
                else:
                    hit_sl, hit_tp = c.high >= sl, c.low <= tp
                if hit_sl or hit_tp:
                    t['outcome'] = 'STOP' if hit_sl else 'TP2'
                    break
            if t['outcome'] == 'OPEN' and now > expiry:
                t['outcome'] = 'EXPIRED'
        except (KeyError, ValueError, TypeError):
            t['outcome'] = 'INVALID'


def stats_message(state: dict) -> str:
    trades = state['paper_trades']
    tp = sum(x.get('outcome') == 'TP2' for x in trades)
    sl = sum(x.get('outcome') == 'STOP' for x in trades)
    op = sum(x.get('outcome') == 'OPEN' for x in trades)
    expired = sum(x.get('outcome') == 'EXPIRED' for x in trades)
    n = tp + sl
    ratio = f'{100*tp/n:.1f} %' if n >= 10 else 'PREMALO PODATKOV (<10 zaključenih)'
    return (f'📊 {VERSION} — PROXY PAPER STATS\n'
            f'Futures testni alerti: {len(trades)} | TP2: {tp} | STOP: {sl} | odprti: {op} | potekli: {expired}\n'
            f'Opazovani TP2 delež: {ratio}\n'
            '⚠️ Le teoretični zadetki nivojev na zapoznelih svečah; ni backtest za MT5, ni upoštevanega izvrševanja, slippage ali provizije.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    group = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token:
        raise SystemExit('Missing TELEGRAM_BOT_TOKEN Actions secret. Do not paste it in chat.')

    now = datetime.now(timezone.utc)
    tg = Telegram(token, client())
    state = state_load()
    commands = []
    updates = tg.updates(state['last_update_id'] + 1)
    for item in updates:
        uid = int(item['update_id'])
        state['last_update_id'] = max(uid, state['last_update_id'])
        msg = item.get('message') or {}
        chat = msg.get('chat') or {}
        target = str(chat.get('id', ''))
        cmd = str(msg.get('text') or '').split(' ', 1)[0].split('@')[0].lower()
        if not group:
            if cmd in ('/id', '/start') and target and chat.get('type') in ('group', 'supergroup'):
                tg.say(target, '✅ ID skupine: ' + target + '\nShrani v GitHub Secret TELEGRAM_CHAT_ID.')
        elif target == group:
            if cmd in ('/scan', '/status', '/help', '/risk', '/stats', '/news', '/id', '/start'):
                commands.append(cmd)
    if not group:
        state_save(state)
        LOG.info('Waiting for Telegram group ID setup')
        return
    commands = commands[-10:]
    if '/id' in commands:
        tg.say(group, f'✅ ID skupine: {group}')
    if '/help' in commands or '/start' in commands:
        tg.say(group, f'🤖 {VERSION}\n/scan — H4/H1/M30/M15/M5, HH/HL, PDH/PDL, liquidity, BOS\n/news — HIGH USD koledar\n/stats — testni rezultati\n/status — stanje\n/risk — risk okvir\nZagoni ~5 min (lahko zamujajo), Yahoo futures vir je zamaknjen.')
    if '/risk' in commands:
        tg.say(group, '🛡️ Primer za račun 15.000 $: 0,25 % = 37,50 $, 0,5 % = 75 $. Velikost lota je odvisna od dejanskega brokerjevega SL in specifikacije instrumenta. Brez lovljenja izgub; največ dve izgubi in konec dneva. Raziskovalni alerts niso trgovalna navodila.')
    if '/status' in commands:
        tg.say(group, f'✅ {VERSION}: ukazi delujejo | {now.astimezone(SI):%d.%m. %H:%M} SI\nSchedule: GitHub lahko zamika ali izpušča zagone. Vir: GC=F / NQ=F, ni MT5 live feed. Najboljši varnostni filter: NO TRADE pri >2,5-minutni zamudi.')
    if '/stats' in commands:
        tg.say(group, stats_message(state))

    session = client()
    news_block, news = get_usd_news(now, session)
    if '/news' in commands:
        tg.say(group, '🗓️ USD NOVICE\n' + news + '\nVir: neuradni gospodarski koledar, ne spremlja X/Twitter. Preveri uradni koledar tudi ročno.')

    report = [f'🔎 {VERSION} — {now.astimezone(SI):%d.%m. %H:%M} SI',
              '⚠️ GC=F/NQ=F futures proxy, NE brokerjev XAUUSD/NDX100.',
              '🗓️ ' + news]
    alerts_sent = 0
    for name, ticker in SYMBOLS.items():
        try:
            df = yahoo_1m(ticker, session, now)
            paper_update(state, ticker, df, now)
            sig, reason = detect_signal(name, ticker, df, now)
            info = market_overview(name, ticker, df, now, reason)
            report.append(format_overview(info))
            if news_block:
                # Fail closed if news source is broken; never invent 'news-safe'.
                continue
            if sig is not None and sig.key not in state['sent_keys']:
                tg.say(group, format_signal(sig, news, True))
                state['sent_keys'] = (state['sent_keys'] + [sig.key])[-300:]
                state['paper_trades'].append({
                    'ticker': sig.ticker, 'direction': sig.direction,
                    'candle_at': sig.candle_at.isoformat(), 'entry': sig.entry,
                    'stop': sig.stop, 'tp2': sig.tp2, 'outcome': 'OPEN',
                })
                state['paper_trades'] = state['paper_trades'][-MAX_TRACKED:]
                alerts_sent += 1
            # A delayed M15 structural heads-up is okay, if unambiguously labelled
            # WATCHLIST with no executable order and no price in the alert.
            elif not info['data_fresh'] and info['age_min'] <= 30:
                b = info['bias']
                sw = info['sweep']
                if b['H1'] in ('BUY', 'SELL') and b['H1'] == b['M30'] and sw:
                    m15 = resample_closed(df, '15min', now)
                    if len(m15) >= 3 and sw[0] >= m15.index[-3]:
                        key = f'watch:{ticker}:{b["H1"]}:{sw[0].isoformat()}'
                        if key not in state['watch_keys']:
                            tg.say(group, format_watch(info))
                            state['watch_keys'] = (state['watch_keys'] + [key])[-200:]
        except Exception as exc:
            LOG.error('Radar %s unavailable: %s', ticker, type(exc).__name__)
            report.append(f'⚠️ {name}: NO TRADE (vir ni na voljo: {type(exc).__name__})')
    if '/scan' in commands:
        tg.say(group, '\n\n'.join(report))
    state_save(state)
    LOG.info('Finished %s; requests=%s; proxy research alerts=%d', VERSION, commands, alerts_sent)


if __name__ == '__main__':
    main()
