# Obsidian Live Trader (Hyperliquid + MEXC)

Lokale FastAPI-App für **Hyperliquid** (Default: **Testnet**) und optional **MEXC USDT-M**: Marktdaten, KI-Analyse (Claude / Grok / Codex / Ollama), Markt-Scanner, Orders nur nach **Apply → Preview → Confirm**.

**Warnung:** Kein Paper-Mode im App-Sinne. Hyperliquid-**Testnet** nutzt Spielgeld; Mainnet und MEXC sind echt. Default: `TRADING_ENABLED=false`.

### Hyperliquid Testnet (empfohlen zum Üben)

1. UI: https://app.hyperliquid-testnet.xyz — Wallet connect, Faucet für Test-USDC  
2. API Wallet / Agent Key erzeugen (kann **nicht** withdrawen)  
3. In `.env`:
   ```env
   EXCHANGE=hyperliquid
   HL_TESTNET=true
   HL_PRIVATE_KEY=0x...
   HL_ACCOUNT_ADDRESS=0x...   # Main-Wallet, falls Agent-Key
   DEFAULT_SYMBOL=BTC
   TRADING_ENABLED=false
   ```
4. `.\start.bat` → Chart mit BTC Testnet-Preis  
5. Smoke: `py -3 scripts\hl_spike.py` und `py -3 scripts\hl_spike.py --with-account`

---

## Setup (Launcher + Einrichtungsassistent)

**Doppelklick `start.bat`** — das reicht für den Normalfall.

| Datei | Zweck |
|--------|--------|
| `start.bat` | Start; **Wizard nur wenn Keys fehlen** |
| `start.bat setup` | Start **mit** Assistent (immer) |
| `setup.bat` | Nur Einrichtungsassistent |
| `scripts/setup_wizard.py` | Interaktive `.env`-Einrichtung (DE) |
| `scripts/launch.py` | Orchestrierung |

Der Assistent fragt u. a.:

1. Börse: **Hyperliquid Testnet** / Mainnet / MEXC  
2. Keys (HL Agent-Key oder MEXC API)  
3. optional xAI  
4. Risiko-Defaults (Hebel, % Risk, Notional)  
5. optional HL-Verbindungstest  

```powershell
cd "C:\Users\Home\Desktop\Trading view"
.\start.bat              # Wizard bei Bedarf, dann Server
.\start.bat setup        # Wizard erzwingen, dann Server
.\setup.bat              # nur Wizard
py -3 scripts\launch.py --setup
py -3 scripts\setup_wizard.py --force
```

Python 3.11+ empfohlen.

**Python-Isolation:** `start.bat` / `launch.py` installieren Pakete **nur** in `.\.venv` (`python -m pip --require-virtualenv`). Die System-/Haupt-Python wird höchstens für `python -m venv` genutzt, nie für globale `pip install`.

---

## Umgebungsvariablen (`.env`)

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `HOST` / `PORT` | `127.0.0.1` / `8787` | Bind (MVP nur localhost) |
| `EXCHANGE` | `hyperliquid` | `mexc` \| `hyperliquid` |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | leer | Futures API — **Trade only, no withdraw** |
| `MEXC_BASE_URL` | `https://contract.mexc.com` | Futures REST |
| `HL_TESTNET` | `true` | Hyperliquid Testnet statt Mainnet |
| `HL_PRIVATE_KEY` / `HL_ACCOUNT_ADDRESS` | leer | Agent/API-Wallet-Key (kann nicht withdrawen) / Main-Wallet |
| `LLM_PROVIDER` | `claude` | `claude` \| `xai` \| `openai` \| `ollama` |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Claude-Modell-ID |
| `XAI_API_KEY` | leer | Grok-Analyse |
| `XAI_MODEL` | `grok-4` | Modell-ID (bei 404 in `.env` wechseln) |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | xAI API |
| `DEFAULT_SYMBOL` | `BTC` | Start-Symbol (Hyperliquid-Coin; MEXC: `BTC_USDT`) |
| **`RISK_PROFILE`** | **`balanced`** | `conservative` \| `balanced` \| `free` — füllt nur nicht explizit gesetzte `MAX_*`/`MIN_*`/`STRICT_*`-Felder |
| `MAX_LEVERAGE` | `50` | App-Hebel-Cap (zusätzlich Contract-Max); Preset `balanced` |
| `MAX_RISK_PCT` | `5.0` | Max. Risiko in % Equity pro Trade; Preset `balanced` |
| `MIN_RRR` | `2.0` | Mindest Risk/Reward |
| `STRICT_RRR` | `false` | `false` = Low-RRR nur Warning (Preset `balanced`); `true` = blockiert |
| **`MAX_NOTIONAL_PCT_OF_EQUITY`** | **`5000.0`** | Harter equity-relativer Notional-Cap (Fat-Finger-Guard), 50× Equity; `0` = aus |
| `PREVIEW_TOKEN_TTL_SECONDS` | `60` | Einmal-Token für Confirm |
| `DATABASE_PATH` | `data/trader.db` | SQLite Audit (Proposals/Orders) |
| **`TRADING_ENABLED`** | **`false`** | **`false` = DISARMED** — kein Place/Confirm. `true` erfordert `LOCAL_API_TOKEN` gesetzt (sonst Startfehler) |
| **`LOCAL_API_TOKEN`** | leer | **Pflicht**, sobald `TRADING_ENABLED=true` — Auth-Token für die lokale API |
| **`ALLOW_UNPROTECTED_ENTRY`** | **`false`** | **`false`** blockiert Orders ohne Stop-Loss |
| `ALLOW_MANUAL_TRIGGER` | `true` | Manueller Trigger-Mode (Entry ohne Börsen-SL/TP); `false` = fail-closed blocken |
| `RISK_SLIPPAGE_PCT` | `0.05` | Slippage-Puffer im Risk-Gate |
| `MAX_NOTIONAL_USDT` | `500` | **Warnschwelle** pro Order (kein Hard-Block) — der harte Notional-Cap ist `MAX_NOTIONAL_PCT_OF_EQUITY` |
| `MAX_PRICE_DRIFT_PCT` | `0.5` | Max. Preisdrift zwischen Preview und Confirm |
| `MARKET_ENTRY_SLIPPAGE_PCT` | `0.15` | Slippage-Toleranz bei Market-Entries |
| `ALLOW_CROSS_MARGIN` | `false` | Cross-Margin statt Isolated erlauben |
| `AUTO_FLATTEN_IF_SL_UNVERIFIED` | `true` | Position sofort schließen, wenn SL nach Placement nicht verifizierbar |
| `SL_VERIFY_ATTEMPTS` | `3` | Anzahl Polling-Versuche, um den gesetzten SL zu bestätigen |
| `SL_VERIFY_DELAY_S` | `0.7` | Wartezeit (s) zwischen SL-Verify-Versuchen |
| `REQUIRE_LOOPBACK_WHEN_ARMED` | `true` | `TRADING_ENABLED=true` erzwingt Loopback-`HOST` |
| `STRICT_AVAILABLE_MARGIN` | `true` | Order gegen tatsächlich verfügbare Margin prüfen (Preset `balanced`) |

Vollständige Vorlage: [`.env.example`](.env.example). Secrets **nie committen**.

### MEXC-Key-Hygiene

1. Neuen API-Key nur für diese App anlegen.
2. **Trade** erlauben, **Withdraw/Transfer deaktivieren** (nicht über API erzwingbar — manuell im MEXC-Account).
3. IP-Whitelist optional; reines Localhost-Limit oft nicht verfügbar → Key-Risiko bewusst halten.
4. Key nur in `.env` auf dem eigenen Rechner.

---

## Start

```powershell
.\start.bat
# oder:
py -3 scripts\launch.py
# oder:
.\.venv\Scripts\python.exe -m app
```

Browser: http://127.0.0.1:8787

- UI: http://127.0.0.1:8787  
- Health: http://127.0.0.1:8787/api/health  

`TRADING_ENABLED` muss in `.env` stehen; Uvicorn-Reload lädt Settings neu (Process-Restart nötig nach `.env`-Änderung, wenn kein Reload).

---

## Flow (kein Autotrade)

0. **Erststart** — ohne `.env` öffnet sich automatisch der Setup-Assistent (`/setup`):
   Börse → KI → Risiko, erzeugt die `.env` und sperrt sich danach selbst.
1. **Laden** — Chart + Indikatoren + Struktur (`GET /api/market/{symbol}`); Coin per Dropdown.
2. **Markt scannen** *(optional)* — `POST /api/scan`: ein günstiges Modell (`SCANNER_MODEL`,
   Default Sonnet) screent die Top-Volumen-Coins in EINEM Call und listet Setups mit Score.
   Klick auf ein Ergebnis öffnet den Coin und startet die Detail-Analyse.
3. **Analyse** — KI-Proposal (`POST /api/analyze`) über `LLM_PROVIDER`
   (Claude Opus / Grok / Codex / Ollama, Hot-Swap per Dropdown); Entry/SL/TP + Levels
   werden als Linien im Chart gezeichnet (TP mit R-Multiple).
4. **Proposal übernehmen** — füllt das Order-Ticket (disabled bei `STAY_OUT`).
5. **Order prüfen (Preview)** — Risk-Gates (`POST /api/orders/preview`); bei OK einmaliges Token.
6. **Confirm LIVE** — nur wenn `TRADING_ENABLED=true` (`POST /api/orders/confirm`); Place mit
   `externalOid`, SL wird nach dem Place auf der Börse verifiziert (sonst Auto-Flatten).
7. **Nach dem Trade** — Position (Entry/Liq) und aktive SL/TP-Trigger erscheinen als
   Chart-Linien; Schließen-Button pro Position; Historie (`GET /api/history`).

Die KI **umgeht keine Gates**. Jeder Ticket wird bei Preview UND Confirm neu validiert.

---

## Manuelle Checkliste

1. **Health** — `GET /api/health`: `ok`, `trading_enabled=false` (default), Keys-Flags.
2. **Market/Chart** — Symbol laden, Kerzen + EMA sichtbar, Kontext (RSI, S/R, funding).
3. **Analyse** — `XAI_API_KEY` gesetzt → Proposal; ohne Key klarer Fehler; Historie zeigt Proposal.
4. **Preview ohne Confirm** — Ticket füllen → Preview-Modal → Abbrechen (kein Place).
5. **Optional Micro-Order** — nur nach Task-0-Spike, `TRADING_ENABLED=true`, minimale Size, mit SL; danach ggf. cancel.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

Alle Tests mocken MEXC/xAI — **keine echten Keys**, kein Live-Order in CI/pytest.

---

## Task 0 Spike (Live-Pfad manuell beweisen)

Vor dem Scharfschalten (`TRADING_ENABLED=true`) den Place/Cancel-Pfad mit Trade-only-Key verifizieren:

```powershell
# Nur Reads (default / dry)
.\.venv\Scripts\python.exe scripts\mexc_spike.py
.\.venv\Scripts\python.exe scripts\mexc_spike.py --dry-read

# Far-Limit Place + Cancel (LIVE, minimale Size)
.\.venv\Scripts\python.exe scripts\mexc_spike.py --place
```

**Nicht** aus pytest aufrufen. Secrets werden redacted; ApiKey/Secret/Signature nicht geloggt.

---

## Risiken Live-Trading

- **Kein Paper-Mode** — jeder Confirm ist echtes Geld.
- **Hebel & Liquidation** — falsch gesetzter SL/Size kann Konto beschädigen.
- **API-Key-Kompromitt** — nur Trade-Recht, kein Withdraw; trotzdem Order-Risiko.
- **MEXC-Feld-/Endpoint-Änderungen** — SL via `stopLossPrice` auf Create; Spike prüfen.
- **Fees/Slippage** — nur teilweise im Gate (Puffer); nicht exakt modelliert.
- **Tokens im Speicher** — Preview-Tokens sterben beim Process-Restart; SQLite-Historie bleibt.
- **`ALLOW_UNPROTECTED_ENTRY=true`** — erlaubt Entries ohne SL; bewusst gefährlich, Default `false`.

---

## API-Kurzüberblick

| Method | Path | Zweck |
|--------|------|--------|
| GET | `/api/health` | Readiness, keine Secrets |
| GET | `/api/market/{symbol}` | OHLCV + Indikatoren + Struktur |
| GET | `/api/account` | Equity / Positionen |
| POST | `/api/analyze` | Grok-Proposal (+ SQLite) |
| POST | `/api/orders/preview` | Gates + One-Time-Token |
| POST | `/api/orders/confirm` | Live Place |
| POST | `/api/orders/cancel` | Cancel by id |
| POST | `/api/orders/close` | Position market-schließen (armed only) |
| GET | `/api/orders/open` | Offene Orders + SL/TP-Trigger |
| POST | `/api/scan` | Markt-Scanner (Top-Coins, 1 LLM-Call) |
| GET/POST | `/api/llm` | KI-Provider lesen / hot-swappen |
| GET | `/api/symbols` | Coin-Liste (Cache + Fallback) |
| POST | `/api/sizing/suggest` | 1 %-Risiko-Größe |
| GET | `/api/history` | Letzte Proposals + Orders |
| GET/POST | `/setup`, `/api/setup` | Erststart-Assistent (nur ohne `.env`) |

---

## Projektstruktur (Auszug)

```
app/
  main.py           # FastAPI routes + lifespan
  config.py         # Settings aus .env
  mexc/             # HMAC client
  analysis/         # Indicators + structure
  llm/              # Grok client + prompts
  risk/             # Gates + sizing
  orders/           # Preview/confirm tokens + service
  db/               # SQLite schema + repo
  static/ templates/
data/trader.db      # Runtime audit (gitignored)
scripts/mexc_spike.py
tests/
```
