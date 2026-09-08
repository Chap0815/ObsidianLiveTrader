# Support

Start with the [quick start](README.md#quick-start-on-windows).
For usage and installation questions, use [Discussions](https://github.com/Chap0815/ObsidianLiveTrader/discussions).
Report reproducible defects in [Issues](https://github.com/Chap0815/ObsidianLiveTrader/issues).

Include your OS, Python version, commit/version, exchange, Testnet/Mainnet mode,
reproduction steps and a short sanitized error excerpt. Never attach `.env`,
private keys, raw databases, account screenshots or unredacted runtime folders.
Security reports follow [SECURITY.md](SECURITY.md).

## Common startup problems

- **Python opens the Microsoft Store:** install Python 3.11+ and disable its
  Windows app-execution aliases. Then run `start.bat` again.
- **Port already in use:** check whether another instance is running. Do not
  run two instances against the same account/database. The default port is 8787.
- **Browser shows old UI:** restart the server, then refresh with Ctrl+F5.
- **Dependency installation fails:** verify network access and Python version.
  After a successful install, `start.bat --skip-install` can reuse the environment.
- **AI unavailable:** check the configured provider and its API access. Manual
  chart viewing and the order ticket do not require AI analysis.

This project offers no guaranteed response time or managed trading service.
If positions are affected, inspect orders and exposure directly at the exchange.
Stopping the application does not itself close positions. Preserve local state;
do not delete the database to dismiss an error.
