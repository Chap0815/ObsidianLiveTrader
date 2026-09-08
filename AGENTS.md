# AGENTS.md

Diese Regeln gelten für das gesamte Repository. Ziel ist eine kleine, lokal
betriebene Trading-Anwendung, deren Money-Path immer fail-closed bleibt.

## Prioritäten

1. Kapital- und Secret-Sicherheit
2. Korrekte Risiko- und Orderlogik
3. Nachvollziehbare, regressionsfreie Änderungen
4. Klare Bedienung und kompakte Dokumentation

## Unverhandelbare Invarianten

- Niemals echte Orders, Cancels oder LLM-Anfragen für Tests oder Audits senden.
- `.env`, private Keys und reale Kontodaten nur auf ausdrücklichen Wunsch lesen;
  niemals Secrets ausgeben, protokollieren oder committen.
- `TRADING_ENABLED=false`, Loopback-Bindung und Mainnet-Bestätigung nicht
  abschwächen.
- Der Orderpfad bleibt `Preview → Confirm`; Confirm prüft alle Gates erneut mit
  frischen Markt- und Kontodaten.
- Unbekannte, fehlende, `NaN`- oder unendliche Finanzwerte blockieren.
- Kein ungeschützter Entry im Standardbetrieb. Stop-Ersatz immer
  `neuen Stop setzen → verifizieren → alten Stop entfernen`.
- Hyperliquid-Limit-Entries bleiben gesperrt, bis ein persistenter,
  idempotenter und restart-sicherer Fill-Watcher existiert.
- Arm, Disarm, Kill-Switch und automatische SL-Mutationen verwenden denselben
  Trade-Lock. Auto-Regeln unmittelbar im Lock erneut prüfen.
- Exchange-Antworten vollständig auswerten, einschließlich verschachtelter
  Batch-Fehler; ein äußeres `status=ok` ist kein Erfolgsbeweis.
- `INCLUDE_ACCOUNT_IN_LLM=false` respektieren. Finanz- und Positionsdetails nur
  nach explizitem Opt-in an externe Provider senden.
- Eine ursprüngliche These nur über die beim Entry gespeicherte Proposal-ID
  zuordnen; niemals einfach den neuesten Vorschlag des Symbols verwenden.
- Journal-Ergebnisse nur mit Historienabdeckung ab `t0` und strikt innerhalb
  des Auswertungsfensters finalisieren.

## Architekturgrenzen

- `app/main.py`: HTTP/WebSocket-Routen, App-Lifecycle und Verdrahtung
- `app/orders/service.py`: einziger zentraler Money-Path
- `app/risk/`: reine Gate- und Sizing-Logik; gemeinsame Formeln wiederverwenden
- `app/hyperliquid/`, `app/mexc/`: Provider-Semantik an der Adaptergrenze
  normalisieren und validieren
- `app/orders/monitor.py`: entscheidet über Auto-Aktionen, umgeht aber niemals
  Service, Trade-Lock oder Schutzverifikation
- `app/db/`: Persistenz und explizite Provenienz
- `app/static/`: Frontend; Servervalidierung bleibt autoritativ

Die App ist ein Single-Worker-System. Keine Änderung darf stillschweigend
mehrere Prozesse, einen verteilten Preview-Store oder parallele Money-Paths
voraussetzen.

## Arbeitsweise

- Vor Änderungen betroffene Module, Tests und `git status --short` prüfen.
- Vorhandene Nutzeränderungen bewahren; keine unrelated Dateien zurücksetzen.
- Kleine, explizite Patches bevorzugen. Sicherheitsregeln nicht duplizieren,
  sondern gemeinsame Helfer verwenden.
- Für jedes behobene Fehlverhalten mindestens einen Negativ- oder Regressionstest
  ergänzen. Race Conditions deterministisch synchronisieren, nicht mit Sleeps.
- Netzwerk und Börse in Tests mocken. Testkonfiguration darf keine lokale `.env`
  laden.
- UI-Änderungen auf 320, 390, 768, 1024, 1280, 1440 und 1920 px prüfen;
  Tastatur-, Modal- und gesperrte Zustände mitprüfen.
- Dokumentation kurz halten: Verhalten und Betriebsgrenzen beschreiben, keine
  Implementierungschronik in die README schreiben.

## Prüfkommandos

Gezielte Tests zuerst, anschließend vor Übergabe:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\ruff.exe check .
node --check app/static/app.js
git diff --check
```

Bei Frontendänderungen zusätzlich:

```powershell
.\.venv\Scripts\python.exe scripts/ui_smoke.py
```

Der Smoke-Test ist offline. Fehlendes Chromium darf installiert werden mit:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Fertig-Definition

Eine Änderung ist erst fertig, wenn:

- die angeforderte Funktion vollständig umgesetzt ist,
- Fehler- und Fail-closed-Pfade getestet sind,
- keine Sicherheitsinvariante geschwächt wurde,
- relevante Tests, Ruff, JS-Syntax und Diff-Check grün sind,
- bei UI-Änderungen der Offline-Smoke-Test grün ist,
- README oder `.env.example` nur bei tatsächlich verändertem Nutzerverhalten
  aktualisiert wurden,
- keine Cache-, Screenshot-, Secret- oder Runtime-Dateien versehentlich im
  Arbeitsbaum liegen.
