"""Вход Sign in with ChatGPT: PKCE, проверка ID token, обновление и отзыв сессии.

Токены лежат в одном локальном профиле вне проекта. В настройках Blender и в
_pipeline_settings.json их нет. Windows шифрует запись через DPAPI, macOS
кладёт её в Связку ключей.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import socket
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
MODELS_URL = "https://api.openai.com/v1/models"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "Подложка DWG"
REFRESH_SKEW_S = 120.0
LOGIN_TIMEOUT_S = 180.0
USAGE_URL = "https://chatgpt.com/codex/settings/usage"
_DEAD_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}

_LOCK = threading.Lock()
_DISCOVERY: dict | None = None
_MODELS: list[dict[str, str]] = []
_NOTICE = ""


class AuthError(Exception):
    """Ошибка входа без токенов в тексте."""


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def granted_scopes(value) -> set[str]:
    if isinstance(value, str):
        return {part for part in value.split() if part}
    if isinstance(value, (list, tuple, set)):
        return {str(part) for part in value if str(part)}
    return set()


def has_direct_scope(value) -> bool:
    return DIRECT_SCOPE in granted_scopes(value)


def token_needs_refresh(expires_at: float, now: float | None = None, skew: float = REFRESH_SKEW_S) -> bool:
    moment = time.time() if now is None else now
    return moment >= float(expires_at) - skew


def oauth_query(query: str) -> dict[str, str]:
    """Query как в браузерном URLSearchParams: «+» остаётся плюсом.

    parse_qs считает плюс пробелом. В коде входа OpenAI плюс тогда превращается
    в пробел, и обмен отвечает invalid_grant.
    """
    found: dict[str, str] = {}
    for part in str(query or "").split("&"):
        if not part:
            continue
        key, separator, value = part.partition("=")
        if not separator:
            continue
        name = urllib.parse.unquote(key)
        if not name or name in found:
            continue
        found[name] = urllib.parse.unquote(value)
    return found


def accept_callback(captured, path: str, expected_state: str) -> None:
    if not isinstance(captured, dict) or path != CALLBACK_PATH:
        raise AuthError("Время входа истекло")
    if captured.get("error"):
        code = _safe(str(captured.get("error") or "access_denied"))
        raise AuthError(auth_error_text(code, str(captured.get("error_description") or "")))
    if captured.get("state") != expected_state:
        raise AuthError("Ответ входа не совпал, повторите попытку")
    if not str(captured.get("code") or ""):
        raise AuthError("Сервер не вернул код входа")


def validate_claims(claims: dict, audience: str, nonce: str, now: float) -> None:
    if claims.get("iss") != ISSUER:
        raise AuthError("Неверный издатель ID token")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, (list, tuple)) else [aud]
    if audience not in audiences:
        raise AuthError("ID token выдан другому приложению")
    if str(claims.get("nonce") or "") != nonce:
        raise AuthError("Ответ входа не совпал")
    try:
        expires = float(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expires = 0
    if expires <= now:
        raise AuthError("ID token истёк")


def storage_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "DWGREADER"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "dwgreader"


def host_id() -> str:
    path = storage_dir() / "host_id.txt"
    if path.is_file():
        saved = path.read_text(encoding="utf-8").strip()
        if saved.startswith("urn:uuid:"):
            return saved
    value = f"urn:uuid:{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


def last_notice() -> str:
    global _NOTICE
    text = _NOTICE
    _NOTICE = ""
    return text


def _set_notice(text: str) -> None:
    global _NOTICE
    _NOTICE = text


def cached_models() -> list[dict[str, str]]:
    global _MODELS
    if not _MODELS:
        saved = (load_session() or {}).get("models")
        if isinstance(saved, list):
            _MODELS = [
                {"slug": str(item["slug"]), "display_name": str(item.get("display_name") or item["slug"])}
                for item in saved
                if isinstance(item, dict) and item.get("slug")
            ]
    return [dict(item) for item in _MODELS]


def remember_model(slug: str) -> None:
    """Выбранная модель лежит в сессии, чтобы перезапуск аддона её не сбрасывал."""
    if not slug or slug == "none":
        return
    session = load_session()
    if not session or session.get("model") == slug:
        return
    session["model"] = slug
    save_session(session)


def saved_model() -> str:
    return str((load_session() or {}).get("model") or "")


def public_profile() -> dict | None:
    session = load_session()
    if not session or not session.get("refresh_token"):
        return None
    return {
        "email": str(session.get("email") or ""),
        "sharing": has_direct_scope(session.get("scopes")),
    }


def load_session() -> dict | None:
    path = storage_dir() / "session.bin"
    if not path.is_file():
        return None
    try:
        payload = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_session(session: dict) -> None:
    path = storage_dir() / "session.bin"
    raw = json.dumps(session, ensure_ascii=False).encode("utf-8")
    _write_secret(path, _protect(raw))


def clear_tokens() -> None:
    session = load_session() or {}
    session.pop("access_token", None)
    session.pop("refresh_token", None)
    session.pop("expires_at", None)
    session.pop("models", None)
    session.pop("model", None)
    session["scopes"] = []
    if session.get("client_id"):
        save_session(session)
        return
    path = storage_dir() / "session.bin"
    if path.is_file():
        path.unlink()


def sign_out() -> None:
    session = load_session()
    if session and session.get("refresh_token") and session.get("client_id"):
        try:
            _revoke(str(session["client_id"]), str(session["refresh_token"]))
        except AuthError:
            pass
    clear_tokens()
    global _MODELS
    _MODELS = []


def ensure_access() -> str:
    """Свежий access token плана ChatGPT или пустая строка, если нужен запасной API."""
    with _LOCK:
        session = load_session()
        if not session or not session.get("refresh_token"):
            return ""
        if not has_direct_scope(session.get("scopes")):
            _set_notice("План ChatGPT не разрешён")
            return ""
        expires_at = float(session.get("expires_at") or 0)
        if not token_needs_refresh(expires_at):
            _set_notice("")
            return str(session.get("access_token") or "")
        try:
            refreshed = _refresh_locked(session)
        except AuthError as error:
            _set_notice(str(error))
            return ""
        if not has_direct_scope(refreshed.get("scopes")):
            _set_notice("План ChatGPT не разрешён")
            return ""
        _set_notice("")
        return str(refreshed.get("access_token") or "")


def force_refresh() -> str:
    """Новый access token после HTTP 401. Пустая строка, если сессии уже нет."""
    with _LOCK:
        session = load_session()
        if not session or not session.get("refresh_token"):
            _set_notice("Сессия ChatGPT недействительна. Войдите ещё раз")
            return ""
        try:
            refreshed = _refresh_locked(session)
        except AuthError as error:
            _set_notice(str(error))
            return ""
        if not has_direct_scope(refreshed.get("scopes")):
            _set_notice("План ChatGPT не разрешён")
            return ""
        _set_notice("")
        return str(refreshed.get("access_token") or "")


def child_credentials(model_slug: str) -> dict[str, str]:
    token = ensure_access()
    slug = (model_slug or "").strip()
    if not token or not slug or slug == "none":
        return {}
    return {
        "DWGREADER_CHATGPT_ACCESS_TOKEN": token,
        "DWGREADER_CHATGPT_MODEL": slug,
    }


class LoginJob:
    def __init__(self) -> None:
        self.done = False
        self.error = ""
        self.email = ""
        self.sharing = False
        self.models: list[dict[str, str]] = []
        self.cancelled = False
        self.another = False
        self.url = ""
        self._server: http.server.HTTPServer | None = None
        self._socket: socket.socket | None = None

    def run(self) -> None:
        try:
            session = _login(self)
            self.email = str(session.get("email") or "")
            self.sharing = has_direct_scope(session.get("scopes"))
            if self.sharing and session.get("access_token"):
                try:
                    self.models = list_models(str(session["access_token"]))
                except AuthError as error:
                    self.error = str(error)
        except AuthError as error:
            self.error = str(error)
        except Exception:
            self.error = "Не удалось войти в ChatGPT"
        finally:
            self.done = True

    def cancel(self) -> None:
        self.cancelled = True
        sock = self._socket
        self._socket = None
        _close_socket(sock)


_ACTIVE_LOGIN: LoginJob | None = None


def set_active_login(job: LoginJob | None) -> None:
    global _ACTIVE_LOGIN
    _ACTIVE_LOGIN = job


def active_login() -> LoginJob | None:
    job = _ACTIVE_LOGIN
    if job is None or job.done:
        return None
    return job


def login_url() -> str:
    job = _ACTIVE_LOGIN
    if job is None or job.done:
        return ""
    return str(job.url or "")


def list_models(access_token: str) -> list[dict[str, str]]:
    global _MODELS
    payload = _get_json(MODELS_URL, bearer=access_token)
    rows: list[dict[str, str]] = []
    source = payload.get("models")
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict) or item.get("visibility", "list") != "list":
                continue
            slug = str(item.get("slug") or "").strip()
            if slug:
                rows.append({"slug": slug, "display_name": str(item.get("display_name") or slug)})
    elif isinstance(payload.get("data"), list):
        for item in payload["data"]:
            if isinstance(item, dict) and item.get("id"):
                slug = str(item["id"])
                rows.append({"slug": slug, "display_name": slug})
    _MODELS = rows
    _store_models(rows)
    if not rows:
        raise AuthError("Список моделей ChatGPT пуст")
    return rows


def _store_models(rows: list[dict[str, str]]) -> None:
    session = load_session()
    if not session:
        return
    session["models"] = rows
    save_session(session)


def refresh_models() -> None:
    """После перезапуска список моделей снова запрашивается, сессия при этом остаётся."""
    token = ensure_access()
    if not token:
        return
    try:
        list_models(token)
    except AuthError:
        return


def _login(job: LoginJob) -> dict:
    saved = load_session() or {}
    try:
        return _authorize(job, saved)
    except AuthError as error:
        issued = str((load_session() or {}).get("client_id") or "")
        # Код регистрации одноразовый. Повтор идёт уже с выданным oaiapp_, не с dynamic_agent_client.
        if getattr(error, "code", "") != "invalid_grant" or not issued.startswith("oaiapp_") or job.cancelled:
            raise
        return _authorize(job, {**saved, "client_id": issued})


def _authorize(job: LoginJob, saved: dict) -> dict:
    issued = str(saved.get("client_id") or "")
    client_id = issued if issued.startswith("oaiapp_") else DYNAMIC_CLIENT
    verifier = new_verifier()
    state = new_secret()
    nonce = new_secret()
    challenge = pkce_challenge(verifier)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    server.timeout = LOGIN_TIMEOUT_S
    server.expected_state = state
    server.captured_ready = False
    job._server = server
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "resource": RESOURCE,
        "ext_agent_host_id": host_id(),
    }
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = AGENT_NAME
    elif saved.get("email") and not job.another:
        params["login_hint"] = str(saved["email"])
    if job.another:
        params["prompt"] = "login"
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    job.url = url
    if not _open_browser(url):
        server.server_close()
        raise AuthError("Не удалось открыть браузер")
    server.timeout = 0.5
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while time.monotonic() < deadline and not job.cancelled:
        server.handle_request()
        if getattr(server, "captured_ready", False):
            break
    server.server_close()
    if job.cancelled:
        raise AuthError("Вход отменён")
    accept_callback(getattr(server, "captured", None), getattr(server, "captured_path", ""), state)
    captured = server.captured
    callback_client = str(captured.get("client_id") or client_id)
    if callback_client == DYNAMIC_CLIENT or not callback_client.startswith("oaiapp_"):
        raise AuthError("Сервер не выдал client_id")
    if client_id.startswith("oaiapp_") and callback_client != client_id:
        raise AuthError("Вход вернул другое приложение. Выйдите и войдите снова")
    _retain_client(callback_client)
    auth_code = str(captured.get("code") or "")
    if not auth_code or " " in auth_code:
        raise AuthError("Сервер не вернул код входа")
    return _finish_authorization(job, {
        "grant_type": "authorization_code",
        "client_id": callback_client,
        "code": auth_code,
        "code_verifier": verifier,
        "redirect_uri": redirect,
        "resource": RESOURCE,
    }, callback_client, nonce, saved)


def _retain_client(client_id: str) -> None:
    """Выданный client_id нужен до обмена: при invalid_grant код уже сгорел, регистрация нет."""
    if not str(client_id).startswith("oaiapp_"):
        return
    session = load_session() or {}
    if session.get("client_id") == client_id:
        return
    save_session({"client_id": client_id})


def _finish_authorization(
    job: LoginJob,
    fields: dict[str, str],
    client_id: str,
    nonce: str,
    previous: dict | None,
) -> dict:
    _raise_if_cancelled(job)
    token = _form(TOKEN_URL, fields, job=job)
    _raise_if_cancelled(job)
    return _store_token_response(token, client_id, nonce, previous=previous, job=job)


def _store_token_response(
    token: dict,
    client_id: str,
    nonce: str,
    previous: dict | None = None,
    job: LoginJob | None = None,
) -> dict:
    _raise_if_cancelled(job)
    access = str(token.get("access_token") or "")
    refresh = str(token.get("refresh_token") or (previous or {}).get("refresh_token") or "")
    id_token = str(token.get("id_token") or "")
    if not access or not refresh or not id_token:
        raise AuthError("Сервер не выдал токены сессии")
    claims = verify_id_token(id_token, client_id, nonce)
    scopes = sorted(granted_scopes(token.get("scope")))
    try:
        expires_in = float(token.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    email = str(claims.get("email") or (previous or {}).get("email") or "")
    session = {
        "client_id": client_id,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token or str((previous or {}).get("id_token") or ""),
        "expires_at": time.time() + expires_in,
        "scopes": scopes,
        "email": email,
        "subject": str(claims.get("sub") or (previous or {}).get("subject") or ""),
    }
    _raise_if_cancelled(job)
    save_session(session)
    return session


def _vendor_dir() -> Path:
    return Path(__file__).resolve().parent / "vendor"


def ensure_vendor_path() -> None:
    """Пакеты из «Установить библиотеки» видны и самому Blender, не только дочернему разбору."""
    vendor = _vendor_dir()
    folder = str(vendor)
    if vendor.is_dir() and folder not in sys.path:
        sys.path.insert(0, folder)


def _import_jwt():
    ensure_vendor_path()
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as error:
        raise AuthError("Сначала нажмите «Установить библиотеки»") from error
    return jwt, PyJWKClient


def ensure_token_checker() -> None:
    """PyJWT нужен, чтобы принять ID token. Грузим его в главном потоке до входа."""
    _import_jwt()


def verify_id_token(token: str, audience: str, nonce: str) -> dict:
    jwt, PyJWKClient = _import_jwt()
    jwks_uri = str(_discovery().get("jwks_uri") or "")
    if not jwks_uri:
        raise AuthError("Не найден ключ проверки OpenAI")
    try:
        key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except AuthError:
        raise
    except Exception as error:
        raise AuthError("ID token не прошёл проверку") from error
    validate_claims(claims, audience, nonce, time.time())
    return claims


def _refresh_locked(session: dict) -> dict:
    client_id = str(session.get("client_id") or "")
    refresh = str(session.get("refresh_token") or "")
    if not client_id or client_id == DYNAMIC_CLIENT or not refresh:
        clear_tokens()
        raise AuthError("Сессия ChatGPT требует повторного входа")
    try:
        token = _form(TOKEN_URL, {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
            "resource": RESOURCE,
        })
    except AuthError as error:
        if getattr(error, "code", "") in _DEAD_REFRESH:
            clear_tokens()
            raise AuthError("Сессия ChatGPT отозвана, войдите снова") from error
        raise
    updated = _apply_refresh(session, token)
    save_session(updated)
    return updated


def _apply_refresh(session: dict, token: dict) -> dict:
    access = str(token.get("access_token") or "")
    if not access:
        raise AuthError("Обновление сессии не вернуло access token")
    updated = dict(session)
    updated["access_token"] = access
    if token.get("refresh_token"):
        updated["refresh_token"] = str(token["refresh_token"])
    if token.get("scope") is not None:
        updated["scopes"] = sorted(granted_scopes(token.get("scope")))
    if token.get("id_token"):
        updated["id_token"] = str(token["id_token"])
    try:
        expires_in = float(token.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    updated["expires_at"] = time.time() + expires_in
    return updated


def _revoke(client_id: str, refresh: str) -> None:
    endpoint = str(_discovery().get("revocation_endpoint") or "")
    if not endpoint:
        raise AuthError("Не найден адрес отзыва сессии")
    _form(endpoint, {
        "token": refresh,
        "token_type_hint": "refresh_token",
        "client_id": client_id,
    })


def _discovery() -> dict:
    global _DISCOVERY
    if _DISCOVERY is None:
        _DISCOVERY = _get_json(DISCOVERY_URL)
    return _DISCOVERY


def _close_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _raise_if_cancelled(job: LoginJob | None) -> None:
    if job is not None and job.cancelled:
        raise AuthError("Вход отменён")


class _LoginHTTPSConnection(http.client.HTTPSConnection):
    """Сокет обмена кода лежит на задании, чтобы «Отмена» оборвала POST."""

    _job: LoginJob | None = None

    def connect(self) -> None:
        job = self._job
        if job is not None and job.cancelled:
            raise OSError("cancelled")
        super().connect()
        if job is None:
            return
        job._socket = self.sock
        if job.cancelled:
            _close_socket(self.sock)
            raise OSError("cancelled")


class _LoginHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, job: LoginJob) -> None:
        super().__init__()
        self._job = job

    def https_open(self, req):
        job = self._job

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
            link = _LoginHTTPSConnection(host, timeout=timeout, **kwargs)
            link._job = job
            return link

        return self.do_open(
            connection,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def _open_form(request: urllib.request.Request, timeout: float, job: LoginJob | None):
    if job is None:
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(_LoginHTTPSHandler(job))
    return opener.open(request, timeout=timeout)


def _form(url: str, fields: dict[str, str], *, job: LoginJob | None = None) -> dict:
    _raise_if_cancelled(job)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _open_form(request, 20, job) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if job is not None and job.cancelled:
            raise AuthError("Вход отменён") from None
        detail = _error_payload(error.read().decode("utf-8", "replace"))
        code = str(detail.get("code") or "")
        failure = AuthError(auth_error_text(code, str(detail.get("message") or "")))
        failure.code = code
        raise failure from None
    except OSError as error:
        if job is not None and job.cancelled:
            raise AuthError("Вход отменён") from None
        raise AuthError("Нет соединения с OpenAI") from error
    finally:
        if job is not None:
            job._socket = None
    _raise_if_cancelled(job)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AuthError("OpenAI вернул не JSON") from error
    if not isinstance(payload, dict):
        raise AuthError("OpenAI вернул неожиданный ответ")
    return payload


def _get_json(url: str, bearer: str = "") -> dict:
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = _error_payload(error.read().decode("utf-8", "replace"))
        raise AuthError(_safe(detail.get("message") or "Список моделей недоступен")) from None
    except (OSError, json.JSONDecodeError) as error:
        raise AuthError("Нет соединения с OpenAI") from error
    if not isinstance(payload, dict):
        raise AuthError("OpenAI вернул неожиданный ответ")
    return payload


def _error_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"message": "Запрос отклонён"}
    if isinstance(payload.get("error"), dict):
        return payload["error"]
    if isinstance(payload.get("error"), str):
        return {"code": payload["error"], "message": str(payload.get("error_description") or payload["error"])}
    return {"message": "Запрос отклонён"}


def auth_error_text(code: str, description: str = "") -> str:
    """Текст для строки состояния. Код OAuth сам по себе пользователю не показываем."""
    reasons = {
        "invalid_grant": (
            "Код входа отклонён: он уже использован или истёк. Войдите ещё раз"
        ),
        "invalid_request": "Запрос входа составлен неверно. Повторите попытку",
        "invalid_client": "Сервер не принял это приложение",
        "unauthorized_client": "Этому приложению нельзя входить через ChatGPT",
        "access_denied": "Вход отменён",
        "invalid_scope": "Запрошенный доступ не разрешён для этого аккаунта",
        "login_required": "Нужно войти в аккаунт ChatGPT в браузере",
        "consent_required": "Нужно подтвердить доступ в окне ChatGPT",
        "interaction_required": "Нужно завершить вход в браузере",
        "server_error": "Сервер OpenAI временно недоступен",
        "temporarily_unavailable": "Сервер OpenAI временно недоступен",
        "invalid_token": "Сессия ChatGPT недействительна. Войдите ещё раз",
        "insufficient_scope": "План ChatGPT не разрешает классификацию через аккаунт",
    }
    known = reasons.get(code)
    if known:
        return known
    cleaned = _safe(description)
    if cleaned and cleaned != code and cleaned != "Запрос отклонён":
        return cleaned
    if code:
        return f"Вход не выполнен ({code})"
    return "Запрос входа отклонён"


def _safe(text: str) -> str:
    cleaned = str(text or "").replace("\n", " ").strip()
    marker = "eyJ"
    while marker in cleaned:
        start = cleaned.find(marker)
        end = start + 3
        while end < len(cleaned) and cleaned[end] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.":
            end += 1
        cleaned = cleaned[:start] + "[скрыто]" + cleaned[end:]
    return cleaned[:180] or "Запрос отклонён"


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = oauth_query(parsed.query)
        expected = str(getattr(self.server, "expected_state", "") or "")
        if query.get("state") != expected:
            body = "<!doctype html><meta charset=utf-8><p>Это не текущее окно входа.</p>".encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not getattr(self.server, "captured_ready", False):
            self.server.captured_path = parsed.path
            self.server.captured = query
            self.server.captured_ready = True
        body = "<!doctype html><meta charset=utf-8><p>Можно вернуться в Blender.</p>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


_KEYCHAIN_MARK = b"keychain"
_KEYCHAIN_SERVICE = b"dwgreader.chatgpt"
_KEYCHAIN_ACCOUNT = b"session"


def _open_browser(url: str) -> bool:
    """Системный браузер. В Blender на macOS webbrowser часто не находит браузер."""
    if sys.platform == "darwin":
        if subprocess.run(["/usr/bin/open", url], check=False).returncode == 0:
            return True
    if sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        if subprocess.run(["xdg-open", url], check=False).returncode == 0:
            return True
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _seal(data: bytes, platform: str) -> bytes:
    if platform == "win32":
        return _dpapi(data, protect=True)
    if platform == "darwin":
        _keychain_save(data)
        return _KEYCHAIN_MARK
    return data


def _open_sealed(data: bytes, platform: str) -> bytes:
    if platform == "win32":
        return _dpapi(data, protect=False)
    if platform == "darwin":
        if data == _KEYCHAIN_MARK:
            return _keychain_load()
        return data
    return data


def _protect(data: bytes) -> bytes:
    return _seal(data, sys.platform)


def _unprotect(data: bytes) -> bytes:
    return _open_sealed(data, sys.platform)


def _keychain_save(data: bytes) -> None:
    import ctypes

    security, core = _security_frameworks(ctypes)
    buffer = ctypes.create_string_buffer(data)
    item = ctypes.c_void_p()
    status = security.SecKeychainAddGenericPassword(
        None,
        len(_KEYCHAIN_SERVICE), _KEYCHAIN_SERVICE,
        len(_KEYCHAIN_ACCOUNT), _KEYCHAIN_ACCOUNT,
        len(data), ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(item),
    )
    if status == -25299:
        _keychain_replace(security, core, ctypes, data)
        return
    if status != 0:
        raise AuthError("macOS не разрешила сохранить сессию в Связке ключей")
    _release(core, item)


def _keychain_replace(security, core, ctypes, data: bytes) -> None:
    length = ctypes.c_uint32()
    password = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(_KEYCHAIN_SERVICE), _KEYCHAIN_SERVICE,
        len(_KEYCHAIN_ACCOUNT), _KEYCHAIN_ACCOUNT,
        ctypes.byref(length), ctypes.byref(password), ctypes.byref(item),
    )
    if status != 0:
        raise AuthError("macOS не разрешила сохранить сессию в Связке ключей")
    buffer = ctypes.create_string_buffer(data)
    try:
        security.SecKeychainItemFreeContent(None, password)
        status = security.SecKeychainItemModifyAttributesAndData(
            item, None, len(data), ctypes.cast(buffer, ctypes.c_void_p),
        )
    finally:
        _release(core, item)
    if status != 0:
        raise AuthError("macOS не разрешила сохранить сессию в Связке ключей")


def _keychain_load() -> bytes:
    import ctypes

    security, core = _security_frameworks(ctypes)
    length = ctypes.c_uint32()
    password = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(_KEYCHAIN_SERVICE), _KEYCHAIN_SERVICE,
        len(_KEYCHAIN_ACCOUNT), _KEYCHAIN_ACCOUNT,
        ctypes.byref(length), ctypes.byref(password), ctypes.byref(item),
    )
    if status != 0:
        raise AuthError("Не удалось открыть сохранённую сессию")
    try:
        return ctypes.string_at(password.value, int(length.value))
    finally:
        security.SecKeychainItemFreeContent(None, password)
        _release(core, item)


def _security_frameworks(ctypes):
    security = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    void = ctypes.c_void_p
    uint = ctypes.c_uint32
    security.SecKeychainAddGenericPassword.argtypes = [
        void, uint, ctypes.c_char_p, uint, ctypes.c_char_p, uint, void, ctypes.POINTER(void),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        void, uint, ctypes.c_char_p, uint, ctypes.c_char_p,
        ctypes.POINTER(uint), ctypes.POINTER(void), ctypes.POINTER(void),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [void, void, uint, void]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [void, void]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core.CFRelease.argtypes = [void]
    core.CFRelease.restype = None
    return security, core


def _release(core, item) -> None:
    if getattr(item, "value", None):
        core.CFRelease(item)


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    incoming = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(incoming), "DWGREADER", None, None, None, 0x01, ctypes.byref(outgoing),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(incoming), None, None, None, None, 0x01, ctypes.byref(outgoing),
        )
    if not ok:
        raise AuthError("Не удалось открыть сохранённую сессию")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _write_secret(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    temporary.replace(path)
    if os.name != "nt":
        os.chmod(path, 0o600)
