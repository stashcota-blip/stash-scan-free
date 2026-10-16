# STASH SCAN FREE — XAU/NASDAQ → Telegram, brez PC-ja

**Status: delujoč programski prototip za raziskovalne signale; NE preverjena profitabilna strategija.**

## Pomembna omejitev (res preberi)
- GitHub Actions se lahko brezplačno zažene v oblaku vsakih **5 minut** v **javnem** repozitoriju. Ni stalno aktiven proces; zagoni se lahko zamaknejo/izpustijo. Po 60 dneh brez aktivnosti GitHub avtomatsko izključi schedule javnega repoja.
- Yahoo Finance podatki `GC=F` (COMEX gold futures) in `NQ=F` (CME Nasdaq-100 futures) so **proxy**, ne XAUUSD ali NDX100 cene na tvojem MetaTrader 5. Lahko so **15+ minut zamaknjeni**, limitirani ali nedosegljivi.
- **Ne uporabljaj Entry / SL / TP številk z GC=F ali NQ=F kot direktnih MT5 naročil.** To ni izvršljiv signal. Deluje kot avtomatsko opozorilo za testiranje algoritma.
- Economic calendar: neuradni javni ForexFactory mirror. Če koledar odpove, med 12:00–18:00 UTC bot konzervativno blokira signale. Novice na X/Twitter niso vključene.
- Ne zaganja tradeov in ni povezan z FundedNext. Ni dokazano, da bi dosegal 60% winrate. Pred trgovanjem potrebuje podatke tvojega brokerja in realen backtest/forward test.
- Javni GitHub repo pomeni da je **koda javna**, Telegram token in chat ID hranimo le med GitHub Secrets (ne v datotekah ali chatu).

## Setup SAMO na iPhonu, brez kartice/VPS

1. Odpri **@BotFather** v Telegramu, pošlji `/newbot`, izberi ime in username, skopiraj **token**. Token je geslo, nikomur ga ne pošiljaj.
2. Naredi Telegram **skupino**, dodaj svojega bota. Za zanesljivo prejemanje ukazov `/scan` in `/id` ga lahko dodaš za administratorja z najmanjšimi pravicami (ne potrebuje brisanja/kickanja članov).
3. Na https://github.com/new ustvari **PUBLIC** repozitorij npr. `stash-scan-free` (Public je pomembno za brezplačne minute). Ne dodajaj privatnih podatkov. Omogoči Actions. GitHub omogoča vse prek Safari z možnostjo "Request Desktop Website".
4. Datoteke iz tega ZIP-a dodaj v repo z **Add file → Upload files**. Pomembno: datoteka `.github/workflows/scan.yml` mora imeti *natančno* to pot. Če Safari ne omogoči upload celotne mape, v GitHub izberi **Add file → Create new file**, kot ime natipkaj `.github/workflows/scan.yml` in vanjo prilepi vsebino iz ZIP-a. Ostale datoteke prenesi v root; `tests/test_engine.py` mora ostati v mapi `tests`.
5. V repozitoriju pojdi v **Settings → Secrets and variables → Actions → New repository secret**. Ustvari `TELEGRAM_BOT_TOKEN` in vanj prilepi token od BotFather. Ne daj ga v README ali workflow! Zaenkrat še ne dodajaj `TELEGRAM_CHAT_ID`.
6. V Telegram skupini pošlji `/id` (če je potrebno `/id@TvojBotUsername`). V GitHub odpri **Actions → STASH SCAN FREE Telegram → Run workflow**. Po koncu bo bot odgovoril s številko skupine (npr. `-100...`).
7. To številko shrani kot drug GitHub repository secret **`TELEGRAM_CHAT_ID`**.
8. Spet klikni **Actions → Run workflow**, nato v skupini pošlji `/scan` in počakaj na naslednji scheduled run (običajno 5–10+ minut). `/status`, `/risk`, `/help` prav tako delujejo na naslednjem zagonu. Če telegram bot ne odgovarja: poglej **Actions → zadnji run → log**.
9. **NE spreminjaj** schedule na 1 minuto — GitHub dopušča najkrajši interval 5 minut, z zamudami.

Če nimaš GitHub računa, ustvari brezplačnega. Za osnovni public repo in GitHub Actions ni potreben VPS, računalnik ali naročnina. Lahko pa GitHub zahteva običajno preverjanje računa. **Ne povezuj FundedNext gesel**.

## Kaj zaznava algoritem
- H1 in M30: EMA20/EMA50 in smer
- M15: zadnji preboj prejšnjih 12-bar visokih/nizkih vrednosti (*liquidity sweep*) in vrnitev v območje
- M5: *break of structure* po M15 sweepu, brez uporabe bodočih sveč
- M1: potrjen retest po M5 preboju
- ATR: strukturna SL razdalja; TP1 1.5R, TP2 2R in preverjanje prostora do M15 nasprotnega nivoja
- Filter visoko pomembnih USD novic (neuradni koledar) ±30 min
- Podvojene signale prepreči `bot_state.json`; zapisuje ID zadnjega ukaza in že poslane sveče
- Če podatki manjkajo, zavrne signal. Ob `/scan` pošlje razlog NO SIGNAL.

## Testiranje lokalno (opcijsko)
```bash
pip install -r requirements.txt
python -m pytest -q
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python telegram_bot.py
```

## Zasebnost in stroški
- **GitHub Actions public**: standardni runnerji brez plačila po uradni politiki; **GitHub Actions private**: GitHub Free vključuje 2.000 minut/mesec in 5-minutni schedule to lahko preseže. Javni repo ne skriva kode, **secrets ostanejo zasebni**.
- Ta sistem je odvisen od GitHub/Yahoo/Telegram/ForexFactory third-party pravil in stabilnosti. Ni SLA in ni garancije za trajno delovanje.
- Repo je brez kartice, vendar **sam ne morem ustvariti tvojega Telegram bota ali povezati tvoje skupine** brez tvojega lastnega BotFather tokena (ki naj ostane samo v Github Secrets).
