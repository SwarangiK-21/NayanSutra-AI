"""Start NyaySutra AI:  python run.py   then open http://127.0.0.1:8000"""
from __future__ import annotations

import logging
import sys
import webbrowser
from threading import Timer

import uvicorn

from nyaya import config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "huggingface_hub", "urllib3", "fastembed", "fontTools", "fpdf"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from nyaya import privacy
    privacy.install_log_filter()   # secrets, tokens and e-mail addresses never reach the log output

    local = config.APP_HOST in ("127.0.0.1", "localhost", "::1")
    accounts = config.auth_mode() == "supabase"
    if not local and not accounts and not config.APP_PASSWORD:
        print("Refusing to listen on a network interface in single-user mode without APP_PASSWORD set in .env "
              "(client documents would be exposed). Configure Supabase Auth, set APP_PASSWORD, or use APP_HOST=127.0.0.1.")
        return 2
    if not local and not config.PUBLIC_URL.startswith("https://"):
        print("Warning: serving on a network interface without HTTPS. Put the app behind a TLS reverse proxy and set "
              "PUBLIC_URL=https://… so session cookies are marked Secure.")
    if not config.OLLAMA_API_KEY and "ollama.com" in config.OLLAMA_HOST:
        print("OLLAMA_API_KEY is missing in .env")
        return 2

    url = f"http://{'127.0.0.1' if local else config.APP_HOST}:{config.APP_PORT}"
    print(f"\n  NyaySutra AI  ->  {url}\n  LLM: {config.LLM_MODEL} (fast: {config.FAST_LLM_MODEL}) via "
          f"{'Ollama Cloud' if 'ollama.com' in config.OLLAMA_HOST else config.OLLAMA_HOST}\n"
          f"  Press Ctrl+C to stop.\n")
    if "--no-browser" not in sys.argv:
        Timer(2.0, lambda: webbrowser.open(url)).start()
    # Client IP / https from X-Forwarded-* only when a reverse proxy we control sits in front (TRUSTED_PROXY_IPS);
    # otherwise a visitor could forge them.
    proxy = {"proxy_headers": True, "forwarded_allow_ips": config.TRUSTED_PROXY_IPS} if config.TRUSTED_PROXY_IPS \
        else {"proxy_headers": False}
    uvicorn.run("nyaya.server:app", host=config.APP_HOST, port=config.APP_PORT, log_level="warning",
                server_header=False, date_header=False, **proxy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
