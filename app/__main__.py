"""Safe launcher: always binds loopback.

  py -3 -m app
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    s = get_settings()
    # Config validator already rejects non-loopback HOST
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
