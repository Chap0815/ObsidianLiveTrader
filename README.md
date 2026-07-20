# Obsidian Live Trader

Ein **lokales Trading-Cockpit** für Krypto-Perpetual-Futures auf **Hyperliquid** (Default: Testnet) und optional **MEXC USDT-M**. Es bündelt Live-Chart + Marktdaten, eine **KI-Setup-Analyse** (Grok / Claude / Codex / Ollama), einen **Markt-Scanner** und einen **server-seitigen Trade-Management-Monitor** — und stellt **eine** Regel über alles: **nichts wird platziert ohne dein bewusstes „Preview → Confirm".**

> ⚠️ **Kein Paper-Mode.** Hyperliquid-**Testnet** ist Spielgeld (zum Üben) — **Mainnet und MEXC sind echtes Geld.** Auslieferungs-Default: `TRADING_ENABLED=false` (entschärft). Real gehandelt wird erst nach bewusstem Scharfschalten (+ bei Mainnet einer Extra-Bestätigung).

**Was die App kann:**
- 📈 **Chart + Kontext in Echtzeit** — Kerzen, EMA/RSI, Support/Resistance, Funding, Open Interest.
- 🤖 **KI-Analyse** — Entry/SL/TP-Vorschlag mit Begründung, einem **Pre-Mortem** (der wahrscheinlichste Fehlergrund vor dem Einstieg) und einer an der **eigenen realen Trefferquote kalibrierten** Confidence (ehrlich, blockt aber nie).
- 🔎 **Markt-Scanner** — screent die aktivsten Coins in einem günstigen LLM-Call und listet Setups mit Score.
- 🛡️ **Harte Risk-Gates** — Risiko-%, RRR, Notional-Cap, **Stop-Pflicht**, Preis-Drift — geprüft bei **jedem** Preview *und* Confirm; die KI umgeht nie ein Gate.
- ⚙️ **Trade-Management-Monitor** — pro Position scharfschaltbares **Auto-Break-Even** (bei +1R Stop auf BE) und **Auto-Trailing** (ATR-Chandelier), dazu Thesis-Invalidierungs- und Time-Stop-Alarme + globaler Kill-Switch. Bewegt einen Stop **nur enger, nie loser**.
- 📓 **KI-Schattenbuch (Journal)** — misst still mit, was funktioniert (nach Setup-Typ & Marktregime), ohne je eine Entscheidung zu erzwingen.

## Inhalt

- [Schnellstart (Testnet)](#schnellstart-testnet)
- [Dein erster Trade — Schritt für Schritt](#dein-erster-trade--schritt-für-schritt)
- [Trade-Management nutzen (Auto-BE / Trailing)](#trade-management-nutzen-auto-be--trailing)
- [Setup & Einrichtungsassistent](#setup-launcher--einrichtungsassistent)
- [Umgebungsvariablen (`.env`)](#umgebungsvariablen-env) · [Wechsel auf Mainnet](#wechsel-auf-mainnet)
- [Betrieb (Update / Backup / Security)](#betrieb) · [Tests](#tests)
- [API-Kurzüberblick](#api-kurzüberblick) · [Projektstruktur](#projektstruktur-auszug) · [Risiken](#risiken-live-trading)

## Schnellstart (Testnet)

**In ~5 Minuten mit Spielgeld startklar:**

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
5. Smoke: `.\.venv\Scripts\python.exe scripts\hl_spike.py` und `.\.venv\Scripts\python.exe scripts\hl_spike.py --with-account`

---

## Dein erster Trade — Schritt für Schritt

Die App handelt **nie** von selbst. Jeder Trade läuft über dieselbe Kette (auf Testnet gefahrlos üben):

1. **Coin laden** — im Dropdown ein Symbol wählen (z. B. `BTC`). Chart, Indikatoren und Struktur erscheinen.
2. **Optional scannen** — Button **Scan**: ein günstiges Modell screent die aktivsten Coins und listet Setups mit Score. Klick auf ein Ergebnis öffnet den Coin und startet direkt die Analyse.
3. **Analysieren** — Button **Analyse**: die KI liefert einen Vorschlag (Entry/SL/TP als Chart-Linien, RRR, Begründung, **Pre-Mortem** und die kalibrierte Confidence). Der Provider ist per Dropdown umschaltbar (produktiv meist Grok).
4. **Übernehmen** — **In Ticket übernehmen** füllt das Order-Ticket (bei `STAY_OUT` bleibt es gesperrt — die KI rät gerade ab). Size/Hebel/Limit kannst du anpassen.
5. **Preview** — **Vorschau**: die Risk-Gates prüfen den Ticket (Risiko-%, RRR, Notional, **Stop-Pflicht**, Drift). Bei OK bekommst du ein **Einmal-Token**; sonst zeigt das Modal genau, welches Gate blockt.
6. **Confirm** — **nur wenn scharf** (`TRADING_ENABLED=true`): **Bestätigen** platziert die Order; der Stop-Loss wird nach dem Place auf der Börse **verifiziert** (schlägt das fehl → Auto-Flatten). Ohne Scharfschaltung endet der Ablauf hier gefahrlos.
7. **Managen** — offene Position (Entry/Liq) und aktive SL/TP-Trigger erscheinen als Chart-Linien; pro Position gibt es Schließen, SL-nachziehen und die Trade-Management-Toggles (siehe unten).

> Merksatz: **Die KI umgeht keine Gates.** Jeder Ticket wird bei Preview **und** Confirm neu validiert — die Analyse ist ein Vorschlag, kein Auto-Trade.

## Trade-Management nutzen (Auto-BE / Trailing)

Ein Monitor läuft server-seitig mit (solange die App läuft) und überwacht offene Positionen. **Standard = nur Alarme.** Pro Position kannst du auf der Positionskarte einzelne Auto-Regeln **scharfschalten** (⚡-Toggles, **nur auf Hyperliquid**):

- **⚡ Auto-BE** — sobald die Position **+1R** (= ein initiales Risiko) im Plus ist, zieht die App den Stop-Loss selbst auf **Break-Even**. Feuert einmal, bewegt den Stop nur in Schutzrichtung.
- **⚡ Auto-Trail** — nach Erreichen der Schwelle zieht die App den Stop dynamisch nach (ATR-Chandelier, `high_water ∓ ATR·Faktor`), immer nur enger, nie loser. Läuft wiederholt.
- **Alarme** (immer an, kein Handeln): **Thesis-Invalidierung** (Preis kreuzt den Invalidierungspunkt der KI-These) und **Time-Stop** (Position läuft lange, geht aber nicht auf).
- **Kill-Switch** — der Button **Alle Auto-Regeln entschärfen** stoppt sofort jede autonome Aktion an allen Positionen.

Alle Auto-Aktionen laufen über denselben abgesicherten `modify-sl`-Pfad (erst neuen Stop platzieren + verifizieren, dann alten canceln — **nie ungeschützt**) und landen im Alarm-Feed („App hat SL auf BE gezogen"). Ohne Scharfschaltung passiert nichts außer Alarmen. Schwellen sind über die `TM_*`-Variablen einstellbar (siehe [Umgebungsvariablen](#umgebungsvariablen-env)).

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
.\.venv\Scripts\python.exe scripts\launch.py --setup
.\.venv\Scripts\python.exe scripts\setup_wizard.py --force
```

Python 3.11+ empfohlen.

**Python-Isolation:** `start.bat` / `launch.py` installieren Pakete **nur** in `.\.venv` (`python -m pip --require-virtualenv`). Die System-/Haupt-Python wird höchstens für `python -m venv` genutzt, nie für globale `pip install`.

**Existiert das `.venv` bereits, braucht `start.bat` gar kein System-`python`** — es nutzt dann direkt `.\.venv\Scripts\python.exe`. Nur beim *allerersten* Anlegen des venv muss ein `py`/`python` im PATH sein.

> **Troubleshooting — „Python nicht gefunden. Bitte Python 3.11+ installieren", obwohl Python (z. B. 3.12) installiert ist:** Dann ist `py`/`python` nur nicht im PATH auffindbar (häufig: Installation ohne „py launcher"/„Add to PATH", oder der Microsoft-Store-Platzhalter). **Sofort-Workaround** (venv vorhanden): `.\.venv\Scripts\python.exe -m app` bzw. `.\.venv\Scripts\python.exe scripts\launch.py`. **Dauerhaft:** Python 3.11+ mit angehaktem „py launcher" + „Add python.exe to PATH" (Reparatur-Installation reicht), oder den Store-Alias unter *Einstellungen → Apps → App-Ausführungsaliase* für `python.exe`/`python3.exe` abschalten; danach `py -3 --version` prüfen.

---

## Umgebungsvariablen (`.env`)

Alle Defaults unten sind 1:1 aus der `Settings`-Klasse in [`app/config.py`](app/config.py) übernommen — bei Abweichung gilt immer der Code. Vollständige, kommentierte Vorlage: [`.env.example`](.env.example). Secrets **nie committen**.

### Server

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `HOST` | `127.0.0.1` | Bind-Adresse; nur Loopback erlaubt (LAN-Bind wird abgelehnt) |
| `PORT` | `8787` | Bind-Port |
| `SETUP_COMPLETE` | `false` (Code-Default) | Markiert Ersteinrichtung als erledigt. `false` hält `/setup` offen (Fail-safe für einen frischen, noch secret-losen Start); die vollständige `/setup`-Speicherung setzt es auf `true` und sperrt den Assistenten danach. **`.env.example` liefert `true`** — eine reine Kopie der Vorlage gilt als bereits eingerichtet |

### Exchange (MEXC / Hyperliquid)

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `EXCHANGE` | `hyperliquid` | `mexc` \| `hyperliquid` |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | leer | Futures API — **Trade only, no withdraw** |
| `MEXC_BASE_URL` | `https://contract.mexc.com` | Futures REST (Host-Allowlist) |
| `HL_TESTNET` | `true` | Hyperliquid Testnet statt Mainnet |
| `HL_PRIVATE_KEY` / `HL_ACCOUNT_ADDRESS` | leer | Agent/API-Wallet-Key (kann nicht withdrawen) / Main-Wallet |
| `HL_BASE_URL` | leer | Override; leer → SDK-Default abhängig von `HL_TESTNET` (Host-Allowlist) |
| `HL_HTTP_TIMEOUT_S` | `10.0` | Hartes Timeout (s) je Hyperliquid-SDK-HTTP-Call; Bereich `[0.5, 120]` |
| **`MAINNET_ACK`** | **`false`** | **Echtgeld-Schutz.** `HL_TESTNET=false` + `TRADING_ENABLED=true` startet **nur** mit `MAINNET_ACK=true` (sonst Startfehler). Nur Hyperliquid (MEXC hat kein Testnet). Siehe [Wechsel auf Mainnet](#wechsel-auf-mainnet) |

### KI / LLM-Provider

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `LLM_PROVIDER` | `claude` | `claude` \| `xai` \| `openai` \| `ollama`; Hot-Swap im UI. **Produktiv läuft die App i. d. R. mit `xai` (Grok) — Claude ist der Code-Default/Fallback** |
| `ANTHROPIC_API_KEY` | leer | Claude-Key (Alias: `CLAUDE_API_KEY`) |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Claude-Modell-ID |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Anthropic API (Host-Allowlist) |
| `ANTHROPIC_VERSION` | `2023-06-01` | Anthropic API-Version-Header |
| `XAI_API_KEY` | leer | Grok-Analyse |
| `XAI_MODEL` | `grok-4` | Modell-ID (bei 404 in `.env` wechseln). Günstigere Varianten `grok-4.3`/`grok-4.5` verfügbar — siehe [Modell-Kostenhebel](#modell-kostenhebel-grok-43--45) |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | xAI API (Host-Allowlist) |
| `OPENAI_API_KEY` | leer | Codex-Key |
| `OPENAI_MODEL` | `gpt-5.1` | OpenAI/Codex-Modell-ID |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API (Host-Allowlist) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Lokales Ollama; nur Loopback erlaubt (SSRF-Schutz) |
| `OLLAMA_MODEL` | `llama3.1` | Ollama-Modell-ID |
| `INCLUDE_ACCOUNT_IN_LLM` | `false` | Opt-in: sendet Equity/verfügbare Margin/offene Positionen als Analyse-Kontext an den externen LLM-Provider |

#### Modell-Kostenhebel (Grok 4.3 / 4.5)

`XAI_MODEL` ist der einzige Null-Code-Kostenhebel im System: `grok-4.3`/`grok-4.5` kosten laut xAI-Preisliste ca. **60–80 % weniger** pro Token als `grok-4`, bei vergleichbarer Analyse-Qualität für dieses Use-Case. Dokumentierter aktueller Default bleibt **`grok-4`** — die Empfehlung ist, `grok-4.3`/`grok-4.5` erst **per Journal-A/B-Vergleich zu verifizieren** (Proposal-Qualität, Trefferquote), **nicht** ungeprüft automatisch umzustellen. Umstellen: `XAI_MODEL=grok-4.3` (oder `grok-4.5`) in `.env`, danach Journal-Fenster beobachten.

### Markt-Scanner

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `SCANNER_MODEL` | `claude-sonnet-5` | Günstiges/schnelles Modell für den Coin-Screen (Detail-Analyse bleibt `LLM_PROVIDER`) |
| `SCANNER_MAX_COINS` | `20` | Top-Volumen-Coins, die gescreent werden |
| `SCANNER_MODE` | `prefilter` | `classic` (alt: Top-N-nach-Turnover in einem LLM-Call) \| `prefilter` (Multi-Ranking-Universum + deterministischer Rules-Prefilter, Chunk-Split/Merge bei vielen Kandidaten) |
| `SCANNER_UNIVERSE_SIZE` | `50` | Kandidaten aus `market_overview` im `prefilter`-Mode (Universum vor Klines); `[1, 500]` |
| `SCANNER_RANK_TOP_N` | `20` | Top-N je Ranking-Dimension (\|Preisänderung\|, \|OI-Δ\|, Volatilität) für die Union; `[1, 500]` |
| `SCANNER_TURNOVER_FLOOR_USD` | `5000000.0` | 24h-Turnover-Liquiditätsfloor (USD); darunter fliegt ein Coin aus dem Universum, egal wie stark er sich bewegt (Wash-/Illiquid-Schutz) |
| `SCANNER_PREFILTER_TOP_K` | `8` | So viele Coins reicht der deterministische Prefilter maximal ans (teure) LLM weiter; `[1, 500]` |
| `SCANNER_LLM_CHUNK_MAX` | `12` | Über so vielen LLM-Kandidaten wird in 2 Chunks gesplittet und gemerged (nur `prefilter`-Mode; `classic` bleibt immer ein Call); `[2, 100]` |

### Risiko-Profil & Gates

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| **`RISK_PROFILE`** | **`balanced`** | `conservative` \| `balanced` \| `free` — füllt nur nicht explizit gesetzte `MAX_*`/`MIN_*`/`STRICT_*`-Felder |
| `MAX_LEVERAGE` | `50` | App-Hebel-Cap (zusätzlich Contract-Max); Preset `balanced`; `[1, 500]` |
| `MAX_RISK_PCT` | `5.0` | Max. Risiko in % Equity pro Trade; Preset `balanced`; `(0, 100]` |
| `MIN_RRR` | `1.5` | Mindest Risk/Reward; Preset `balanced`; `[0, 1000]` |
| `STRICT_RRR` | `false` | `false` = Low-RRR nur Warning (Preset `balanced`); `true` = blockiert |
| `STRICT_AGGREGATE_RISK` | `false` | Portfolioweiter Aggregat-Risiko-Cap (statt nur pro Position); `conservative` setzt das automatisch auf `true` |
| `AGGREGATE_POS_RISK_CAP_PCT` | `2.0` | Cap für Aggregat-Risiko in % Equity, falls `STRICT_AGGREGATE_RISK=true`; `(0, 100]` |
| **`MAX_NOTIONAL_PCT_OF_EQUITY`** | **`5000.0`** | Harter equity-relativer Notional-Cap (Fat-Finger-Guard), 50× Equity; `0` = aus; `[0, 1000000]` |
| `MAX_NOTIONAL_USDT` | `500.0` | **Warnschwelle** pro Order (kein Hard-Block) — der harte Notional-Cap ist `MAX_NOTIONAL_PCT_OF_EQUITY` |
| `MAX_PRICE_DRIFT_PCT` | `1.0` | Max. Preisdrift zwischen Preview und Confirm; `[0, 100]` |
| `MARKET_ENTRY_SLIPPAGE_PCT` | `0.15` | Slippage-Toleranz bei Market-Entries; `[0, 100]` |
| `RISK_SLIPPAGE_PCT` | `0.05` | Slippage-Puffer im Risk-Gate; `[0, 100]` |
| `STRICT_AVAILABLE_MARGIN` | `true` | Order gegen tatsächlich verfügbare Margin prüfen (Preset `balanced`) |
| `ALLOW_CROSS_MARGIN` | `false` | Cross-Margin statt Isolated erlauben |
| **`ALLOW_UNPROTECTED_ENTRY`** | **`false`** | **`false`** blockiert Orders ohne Stop-Loss |
| `ALLOW_MANUAL_TRIGGER` | `false` | Manueller Trigger-Mode (Entry ohne Börsen-SL/TP); `false` = fail-closed blocken (bereits im Preview-Gate, R-04) |

### Trading / Betrieb

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| **`TRADING_ENABLED`** | **`false`** | **`false` = DISARMED** — kein Place/Confirm. `true` erfordert `LOCAL_API_TOKEN` gesetzt (sonst Startfehler) |
| **`LOCAL_API_TOKEN`** | leer | **Pflicht**, sobald `TRADING_ENABLED=true` — Auth-Token für die lokale API |
| `REQUIRE_LOOPBACK_WHEN_ARMED` | `true` | `TRADING_ENABLED=true` erzwingt Loopback-`HOST` |
| `DEFAULT_SYMBOL` | `BTC` | Start-Symbol (Hyperliquid-Coin; MEXC: `BTC_USDT`) |
| `PREVIEW_TOKEN_TTL_SECONDS` | `60` | Einmal-Token für Confirm |
| `DATABASE_PATH` | `data/trader.db` | SQLite Audit (Proposals/Orders) |
| `KLINE_LIMIT_HINT` | `500` | Bevorzugte Anzahl Kerzen je Marktabruf |
| `AUTO_FLATTEN_IF_SL_UNVERIFIED` | `true` | Position sofort schließen, wenn SL nach Placement nicht verifizierbar |
| `SL_VERIFY_ATTEMPTS` | `3` | Anzahl Polling-Versuche, um den gesetzten SL zu bestätigen |
| `SL_VERIFY_DELAY_S` | `0.7` | Wartezeit (s) zwischen SL-Verify-Versuchen; `[0, 60]` |
| `CLOSE_VERIFY_ATTEMPTS` | `1` | Polling-Versuche beim Re-Read der Position nach einem Close (deckt verzögerte Fills ab) |
| `CLOSE_VERIFY_DELAY_S` | `0.0` | Wartezeit (s) zwischen Close-Verify-Versuchen |

### Journal (KI-Feedback-Loop)

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `JOURNAL_ENABLED` | `true` | KI-Schattenbuch (Advisory/Messung); beeinflusst nie den Order/Gate/Confirm-Pfad |
| `JOURNAL_RESOLVE_INTERVAL_S` | `60` | Poll-Intervall (s), um offene Journal-Einträge gegen den Markt aufzulösen |
| `JOURNAL_WINDOW_HOURS` | `24` | Betrachtungsfenster (h) für die Journal-Auswertung |
| `JOURNAL_MIN_SAMPLE` | `20` | Mindestanzahl Samples, bevor Journal-Statistiken angezeigt werden |
| `TM_ENABLED` | `true` | Trade-Management-Monitor (Auto-BE + Alarme). Auto-Aktionen NUR bei pro Position scharfgeschalteter Regel; ohne Arming nur Alarme |
| `TM_MONITOR_INTERVAL_S` | `20` | Monitor-Poll-Intervall (s); Bereich `[5, 300]` |
| `TM_BE_TRIGGER_R` | `1.0` | Auto-BE feuert ab diesem unrealisierten R-Vielfachen; `[0.1, 10]` |
| `TM_BE_FEE_RT` | `0.0006` | Round-Trip-Fee-Puffer für die Break-Even-Berechnung; `[0, 0.01]` |
| `TM_TIME_STOP_HOURS` | `4.0` | Time-Stop-Alarm, wenn die Position so lange läuft; `[0.25, 168]` |
| `TM_TIME_STOP_MIN_R` | `0.5` | Time-Stop-Alarm nur, wenn die Position unter diesem R steht; `[-5, 10]` |
| `TM_TRAIL_ATR_MULT` | `2.0` | Auto-Trail: Chandelier-Abstand = dieses Vielfache des ATR; `[0.5, 10]` |
| `TM_TRAIL_ACTIVATION_R` | `1.0` | Auto-Trail aktiviert erst ab diesem unrealisierten R; `[0, 10]` |
| `TM_TRAIL_ATR_PERIOD` | `14` | ATR-Periode (Wilder) für den Trail; `[2, 100]` |
| `TM_TRAIL_ATR_TF` | `15m` | Timeframe der ATR-Kerzen für den Trail; eine von `5m, 15m, 1h, 4h` |
| `TM_RECAL_MIN_SAMPLE` | `20` | Confidence-Rekalibrierung: Mindest-Samples je Tier, bevor die reale Trefferquote die *angezeigte* Confidence/Sizing-Empfehlung anpasst; `[1, 1000]` |

**Trade-Management-Layer (v1):** ein server-seitiger Monitor läuft mit dem Prozess
und überwacht offene Positionen. Standard = nur **Alarme** (Thesis-Invalidierung,
Time-Stop). Pro Position kann im Dashboard **Auto-BE** scharfgeschaltet werden —
dann zieht die App den Stop-Loss bei `+TM_BE_TRIGGER_R` R selbst auf Break-Even.
Auto-BE ist **Hyperliquid-only**, feuert **einmal** pro Position, bewegt den Stop
**nur in Schutzrichtung** (nie lockern) und läuft über den bestehenden
`modify-sl`-Pfad (never-unprotected, `trade_lock`, Audit). Endpunkte (alle
`require_local_token`): `POST /api/positions/arm` `{symbol, side, rules:{auto_be:bool, auto_trail:bool}}`,
`GET /api/positions/alerts` (Polling-Feed für UI), `POST /api/positions/killswitch`
(entschärft sofort ALLE Positionen). Bekannte Grenzen (Spec §10): App-Neustart
mitten im Trade nimmt den aktuellen SL als Baseline; ein Close→Reopen bei nahezu
identischem Entry innerhalb der 2-Zyklen-Absenz-Grace kann die alte Baseline erben.

**Trade-Management-Layer (v2 — Auto-Trailing):** zusätzlich zu Auto-BE kann pro
Position **Auto-Trail** scharfgeschaltet werden — ein ATR-Chandelier-Trailing-Stop
(`high_water ∓ TM_TRAIL_ATR_MULT · ATR`), der ab `TM_TRAIL_ACTIVATION_R` R greift.
Wie Auto-BE ist er **opt-in pro Position**, **Hyperliquid-only**, bewegt den Stop
**nur in Schutzrichtung** (nie lockern) und läuft über denselben `modify-sl`-Pfad.
Er feuert **wiederholt** (kein Einmal-Latch), jeder Zug ist monoton enger. Der
Time-Stop bleibt reiner **Alarm** (kein autonomes Schließen). Kosten: **+1
`klines`-Fetch pro scharfgeschalteter Trail-Position und Zyklus** (pro Symbol/TF
gecacht).

**KI-Kalibrierung & Ehrlichkeit (advisory, ändert NIE eine Entscheidung):** Der
Analyze-Pfad kennzeichnet jeden Trade mit einem **Regime-Tag** (BTC-Trend ×
Volatilität) im Journal für spätere Segmentierung (`by_regime`). Aus der eigenen
realen Trefferquote (Wilson-**Untergrenze**, ab `TM_RECAL_MIN_SAMPLE` Samples)
**rekalibriert** die App die *angezeigte* Confidence und die *Sizing-Empfehlung*
(Downgrade + kleinere Empfehlung bei schwacher Quote) — die rohe KI-`setup_confidence`
bleibt sichtbar, und es wird **nie** ein Trade geblockt oder `action` geändert
(zusätzliche Response-Felder `confidence_calibrated`/`calibration_note`/`size_factor`;
der echte Order-/Gate-Pfad ist unberührt). Jede Analyse nennt zudem ein
**Pre-Mortem** (`pre_mortem`): den einen wahrscheinlichsten Grund, warum der Trade
scheitert — vor dem Einstieg.

### Wechsel auf Mainnet

Der Sprung von Hyperliquid-**Testnet** (Spielgeld) auf **Mainnet** (echtes Kapital) ist der gefährlichste Moment: ein stilles `HL_TESTNET=false` reicht sonst, während `TRADING_ENABLED=true` aus der Testnet-Phase scharf bleibt. Deshalb ist ein scharfer Mainnet-Start **fail-closed** und wird abgelehnt, bis du ihn mit `MAINNET_ACK=true` einmalig bestätigst. Ablauf:

1. **Trading disarmen** — `TRADING_ENABLED=false` setzen, bevor du irgendetwas an der Börsen-Konfiguration änderst.
2. **Auf Mainnet umstellen** — `HL_TESTNET=false`; echten Mainnet-Agent-Key in `HL_PRIVATE_KEY` (bzw. `HL_ACCOUNT_ADDRESS` bei Agent-/API-Wallet) hinterlegen.
3. **Agent-Key-Scope prüfen** — der Key darf **nur traden, nicht abheben** (kein Withdraw-Scope). Siehe [Task 0 Spike](#task-0-spike-live-pfad-manuell-beweisen).
4. **Micro-Probe** — mit minimaler Size + SL einen Place/Cancel auf Mainnet beweisen (Task-0-Spike gegen Mainnet), damit Keys/Netz/Adresse stimmen.
5. **Bestätigen & armen** — `MAINNET_ACK=true` **und** `TRADING_ENABLED=true` setzen, App neu starten. Im UI erscheint dann der rote Chip **MAINNET · ECHTGELD**, im Log eine `MAINNET · ECHTGELD AKTIV`-Zeile.

Wieder auf Testnet zurück (`HL_TESTNET=true`) deaktiviert das Gate automatisch — `MAINNET_ACK` darf gesetzt bleiben, greift aber nur bei Mainnet.

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
.\.venv\Scripts\python.exe scripts\launch.py
# oder:
.\.venv\Scripts\python.exe -m app
```

Browser: http://127.0.0.1:8787

- UI: http://127.0.0.1:8787  
- Health: http://127.0.0.1:8787/api/health  

`TRADING_ENABLED` muss in `.env` stehen; Uvicorn-Reload lädt Settings neu (Process-Restart nötig nach `.env`-Änderung, wenn kein Reload).

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

## Betrieb

**Update (3 Zeilen):**

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\start.bat
```

**Backup:** Vor jedem Update/Update-Test den Server **stoppen**, dann `data/` (SQLite: Proposals/Orders/Journal) **und** `.env` (Secrets + Konfiguration) an einen sicheren Ort kopieren. Beides sind die einzigen Dinge, die einen Neuaufsatz nicht überleben würden — Code selbst ist per `git` reproduzierbar.

**Logs:** Aktuell schreiben alle Logger (`app.main`, `app.llm.client`, `app.llm.scanner`, `app.realtime.hl`, `app.journal.resolver`, `app.env_builder`) nur nach stdout/stderr (Uvicorn-Standard) — nichts wird persistent auf Platte gehalten. Für dauerhafte Logs optional einen `logging.handlers.RotatingFileHandler` (z. B. `maxBytes=5_000_000, backupCount=5`) auf den Root-Logger registrieren; damit rotieren Logdateien automatisch statt unbegrenzt zu wachsen.

**Security-Check (empfohlen, periodisch):**

```powershell
.\.venv\Scripts\python.exe -m pip install pip-audit
.\.venv\Scripts\python.exe -m pip_audit -r requirements.lock
```

Prüft alle gepinnten Dependencies gegen bekannte CVEs; vor jedem Mainnet-Wechsel oder mindestens monatlich laufen lassen.

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
| POST | `/api/orders/modify-sl` | Stop-Loss nachziehen (place→verify→cancel, never-unprotected) |
| GET | `/api/orders/open` | Offene Orders + SL/TP-Trigger |
| POST | `/api/positions/arm` | Auto-Regel pro Position scharfschalten (`{auto_be, auto_trail}`) |
| GET | `/api/positions/alerts` | Trade-Management-Alarm- + Auto-Aktions-Feed (Polling) |
| POST | `/api/positions/killswitch` | Alle Auto-Regeln sofort entschärfen |
| POST | `/api/analyze` · `/api/reevaluate` | KI-Proposal / offene These neu bewerten |
| POST | `/api/scan` | Markt-Scanner (Top-Coins, 1 LLM-Call) |
| GET/POST | `/api/llm` | KI-Provider lesen / hot-swappen |
| GET | `/api/symbols` | Coin-Liste (Cache + Fallback) |
| POST | `/api/sizing/suggest` | Risiko-basierte Größen-Empfehlung |
| GET | `/api/history` · `/api/journal` | Letzte Proposals/Orders · KI-Schattenbuch-Statistik |
| GET/POST | `/setup`, `/api/setup` | Erststart-Assistent (nur ohne Keys) |

---

## Projektstruktur (Auszug)

```
app/
  main.py           # FastAPI-Routen + Lifespan (Background-Tasks: Journal-Resolver, Trade-Monitor)
  config.py         # Settings aus .env (mit Validatoren)
  models.py         # Pydantic-Modelle (OrderTicket, TradeProposal …)
  security.py       # CSP, Loopback-/Token-Guard, Symbol-Normalisierung
  hyperliquid/      # Hyperliquid-Client (SDK-Wrapper) + Fehlerklassen
  mexc/             # MEXC-USDT-M-Client (HMAC) + Fehlerklassen
  analysis/         # Indikatoren (ATR/RSI/EMA), Struktur, Markt-Kontext
  llm/              # LLM-Clients + Prompts + Scanner + Confidence-Recalibration
  risk/             # Risk-Gates + Sizing
  orders/           # service.py (Place/Modify-SL/Close), monitor.py (Trade-Management-Loop),
                    #   trade_manager.py (reine Regeln), be_math.py, protection.py, tokens.py
  journal/          # KI-Schattenbuch: Resolver + Statistik (Wilson-CI)
  db/               # SQLite-Schema + Repository (Proposals/Orders/Journal/Position-Mgmt)
  realtime/         # Hyperliquid-WebSocket-Proxy
  static/           # store.js → utils.js → trade-math.js → api.js → app.js  (+ app.css, Fonts)
  templates/        # base.html, dashboard.html, setup.html
scripts/            # launch.py, setup_wizard.py, hl_spike.py, mexc_spike.py
tests/              # pytest — alles gemockt, keine echten Keys / Live-Orders
docs/superpowers/   # Design-Specs + Bau-Pläne (Historie)
data/trader.db      # Runtime-Audit + Journal (gitignored)
```
