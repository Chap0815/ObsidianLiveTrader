# Third-party notices

Project licensing does not replace the licenses of bundled or installed
dependencies.

| Bundled component | Location | License and notice |
| --- | --- | --- |
| TradingView Lightweight Charts 4.2.0 | `app/static/vendor/` | [Apache 2.0](LICENSES/Lightweight-Charts-Apache-2.0.txt), [NOTICE](LICENSES/Lightweight-Charts-NOTICE.txt) |
| IBM Plex Sans and Mono | `app/static/fonts/` | [SIL Open Font License 1.1](LICENSES/IBM-Plex-OFL-1.1.txt) |

Charts use [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/).
The existing chart attribution and upstream source headers are retained.
Obsidian Live Trader is an independent project.

Python packages listed in [`requirements.lock`](requirements.lock) and development
tools in [`requirements-dev.lock`](requirements-dev.lock) retain their respective
licenses. They are downloaded
during installation rather than included as vendored Python packages here.
The `.txt` requirement files describe direct version ranges; the `.lock` files
identify the tested installation with exact versions and artifact hashes.
Hashes and project licensing do not replace an upstream component's terms.

The project terms and personal-trading permission are in [LICENSE](LICENSE).
The unmodified base license and bundled component license/notice files are
retained under [`LICENSES/`](LICENSES/).
