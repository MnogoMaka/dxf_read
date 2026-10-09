"""Пробный запрос к модели, тот же, которым плагин проверяет, спит ли она.

Адрес, имя и ключ берутся из .env: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY.
Свой текст вместо ok:  python api_test.py "привет"
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error

from dxf_read import config
from dxf_read.dxf import _CONNECTION_ERRORS, _llm_headers, _open_json

# Промпт пробуждения из _ensure_model_awake.
PROMPT = "ok"
MAX_TOKENS = 1
TIMEOUT_S = 40.0


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or PROMPT
    max_tokens = MAX_TOKENS if prompt == PROMPT else 256
    cfg = config.settings()
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": cfg.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    print(f"POST {url}")
    print(f"model={cfg.llm_model}")
    print(f"prompt={prompt!r}")
    started = time.monotonic()
    try:
        response = _open_json(url, body, _llm_headers(cfg.llm_api_key), TIMEOUT_S)
        with response:
            raw = response.read().decode("utf-8", "replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        elapsed = time.monotonic() - started
        detail = error.read().decode("utf-8", "replace")[:500]
        print(f"Модель ответила HTTP {error.code} за {elapsed:.1f} с — она не спит.")
        print(detail)
        return 1
    except _CONNECTION_ERRORS as error:
        elapsed = time.monotonic() - started
        print(f"Модель спит или сервер молчит: {elapsed:.1f} с, {error}")
        return 2

    elapsed = time.monotonic() - started
    print(f"Модель запущена, HTTP {status}, {elapsed:.1f} с")
    try:
        payload = json.loads(raw)
        text = payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        print(raw[:1000])
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
