from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

import chatgpt_auth
from dxf_read.config import PipelineSettings, use_settings
from dxf_read.dxf import (
    ChatGPTNeedChoice,
    ChatGPTRouteError,
    _request_responses_once,
    _resolved_api_key,
    _response_text_from_sse,
    select_target_layers,
)

_PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_GREEN = '{"green": ["Газон"]}'


def _sse(text: str) -> list[bytes]:
    payload = json.dumps({"type": "response.output_text.delta", "delta": text}, ensure_ascii=False)
    done = json.dumps({"type": "response.completed"})
    return [
        b"event: response.output_text.delta\n",
        f"data: {payload}\n".encode("utf-8"),
        b"\n",
        b"event: response.completed\n",
        f"data: {done}\n".encode("utf-8"),
        b"\n",
    ]


class _Stream:
    def __init__(self, lines: list[bytes], request_box: dict, request) -> None:
        self._lines = lines
        request_box["body"] = json.loads(request.data.decode("utf-8"))
        request_box["authorization"] = request.get_header("Authorization")

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class PkceTests(unittest.TestCase):
    def test_s256_matches_rfc_vector(self) -> None:
        self.assertEqual(chatgpt_auth.pkce_challenge(_PKCE_VERIFIER), _PKCE_CHALLENGE)

    def test_state_mismatch_is_rejected(self) -> None:
        with self.assertRaises(chatgpt_auth.AuthError):
            chatgpt_auth.accept_callback(
                {"state": "other", "code": "abc"},
                chatgpt_auth.CALLBACK_PATH,
                "expected",
            )

    def test_mac_opens_the_system_browser(self) -> None:
        with patch.object(chatgpt_auth.sys, "platform", "darwin"), patch.object(
            chatgpt_auth.subprocess, "run",
        ) as run:
            run.return_value.returncode = 0
            self.assertTrue(chatgpt_auth._open_browser("https://auth.openai.com/example"))
        self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/open", "https://auth.openai.com/example"])

    def test_matching_callback_is_accepted(self) -> None:
        chatgpt_auth.accept_callback(
            {"state": "expected", "code": "abc", "client_id": "oaiapp_test"},
            chatgpt_auth.CALLBACK_PATH,
            "expected",
        )


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(chatgpt_auth, "storage_dir", return_value=Path(self.tmp.name))
        self.patcher.start()
        chatgpt_auth._NOTICE = ""

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tmp.cleanup()

    def _session(self, **extra) -> dict:
        record = {
            "client_id": "oaiapp_test",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": time.time() + 3600,
            "scopes": ["openid", "chatgpt.tokens.use.direct"],
            "email": "user@example.com",
        }
        record.update(extra)
        chatgpt_auth.save_session(record)
        return record

    def test_mac_session_stays_in_the_keychain(self) -> None:
        stored: dict[str, bytes] = {}

        def save(data: bytes) -> None:
            stored["data"] = data

        def load() -> bytes:
            return stored["data"]

        secret = b'{"access_token":"old-access","refresh_token":"old-refresh"}'
        with patch.object(chatgpt_auth, "_keychain_save", save), patch.object(
            chatgpt_auth, "_keychain_load", load,
        ):
            sealed = chatgpt_auth._seal(secret, "darwin")
            opened = chatgpt_auth._open_sealed(sealed, "darwin")
        self.assertEqual(sealed, b"keychain")
        self.assertNotIn(b"access_token", sealed)
        self.assertEqual(opened, secret)
        self.assertEqual(chatgpt_auth._open_sealed(secret, "darwin"), secret)

    def test_linux_session_keeps_file_permissions_path(self) -> None:
        secret = b'{"access_token":"old-access"}'
        self.assertEqual(chatgpt_auth._seal(secret, "linux"), secret)

    def test_saved_record_is_not_plaintext(self) -> None:
        self._session()
        raw = (Path(self.tmp.name) / "session.bin").read_bytes()
        self.assertNotIn(b"access_token", raw)
        self.assertNotIn(b"old-refresh", raw)
        loaded = chatgpt_auth.load_session()
        assert loaded is not None
        self.assertEqual(loaded["access_token"], "old-access")

    def test_host_id_stays_stable(self) -> None:
        self.assertEqual(chatgpt_auth.host_id(), chatgpt_auth.host_id())
        self.assertTrue(chatgpt_auth.host_id().startswith("urn:uuid:"))

    def test_fresh_token_is_reused(self) -> None:
        self._session()
        with patch.object(chatgpt_auth, "_form") as form:
            self.assertEqual(chatgpt_auth.ensure_access(), "old-access")
        form.assert_not_called()

    def test_missing_scope_falls_back(self) -> None:
        self._session(scopes=["openid"])
        self.assertEqual(chatgpt_auth.ensure_access(), "")
        self.assertIn("не разрешён", chatgpt_auth.last_notice())
        self.assertEqual(chatgpt_auth.child_credentials("gpt-test"), {})

    def test_refresh_replaces_rotating_token(self) -> None:
        self._session(expires_at=time.time() - 10)

        def fake_form(url, fields):
            self.assertEqual(fields["grant_type"], "refresh_token")
            self.assertEqual(fields["client_id"], "oaiapp_test")
            self.assertEqual(fields["resource"], chatgpt_auth.RESOURCE)
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "openid chatgpt.tokens.use.direct",
            }

        with patch.object(chatgpt_auth, "_form", side_effect=fake_form):
            self.assertEqual(chatgpt_auth.ensure_access(), "new-access")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["access_token"], "new-access")

    def test_dead_refresh_clears_tokens_and_falls_back(self) -> None:
        self._session(expires_at=time.time() - 10)

        def fake_form(url, fields):
            error = chatgpt_auth.AuthError("invalid_grant")
            error.code = "invalid_grant"
            raise error

        with patch.object(chatgpt_auth, "_form", side_effect=fake_form):
            self.assertEqual(chatgpt_auth.ensure_access(), "")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertNotIn("refresh_token", saved)
        self.assertEqual(saved["client_id"], "oaiapp_test")
        self.assertIn("войдите снова", chatgpt_auth.last_notice())

    def test_invalid_grant_is_a_sentence(self) -> None:
        text = chatgpt_auth.auth_error_text("invalid_grant")
        self.assertNotEqual(text, "invalid_grant")
        self.assertIn("Войдите ещё раз", text)
        self.assertNotIn("план", text.lower())
        self.assertEqual(chatgpt_auth.auth_error_text("access_denied"), "Вход отменён")

    def test_authorization_code_keeps_a_plus(self) -> None:
        parsed = chatgpt_auth.oauth_query("code=ab%2Bcd&state=s1&client_id=oaiapp_test")
        self.assertEqual(parsed["code"], "ab+cd")
        self.assertEqual(chatgpt_auth.oauth_query("code=ab+cd&state=s1")["code"], "ab+cd")

    def test_invalid_grant_retries_with_the_issued_client(self) -> None:
        calls: list[str] = []

        def authorize(job, saved):
            calls.append(str(saved.get("client_id") or ""))
            if len(calls) == 1:
                chatgpt_auth.save_session({"client_id": "oaiapp_issued"})
                error = chatgpt_auth.AuthError("отклонён")
                error.code = "invalid_grant"
                raise error
            return {"email": "user@example.com", "scopes": ["openid"], "access_token": "new"}

        with patch.object(chatgpt_auth, "_authorize", side_effect=authorize):
            session = chatgpt_auth._login(chatgpt_auth.LoginJob())
        self.assertEqual(calls, ["", "oaiapp_issued"])
        self.assertEqual(session["access_token"], "new")

    def test_cancel_before_exchange_skips_the_token_request(self) -> None:
        self._session()
        job = chatgpt_auth.LoginJob()
        job.cancel()
        with patch.object(chatgpt_auth, "_form", side_effect=AssertionError("form")):
            with self.assertRaises(chatgpt_auth.AuthError) as caught:
                chatgpt_auth._finish_authorization(job, {}, "oaiapp_test", "nonce", None)
        self.assertEqual(str(caught.exception), "Вход отменён")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertEqual(saved["access_token"], "old-access")

    def test_cancel_during_exchange_keeps_the_saved_token(self) -> None:
        self._session()
        job = chatgpt_auth.LoginJob()

        def fake_form(url, fields, *, job=None):
            job.cancel()
            return {
                "access_token": "leaked",
                "refresh_token": "leaked",
                "id_token": "id-token",
                "expires_in": 60,
            }

        previous = chatgpt_auth.load_session()
        with patch.object(chatgpt_auth, "_form", side_effect=fake_form):
            with self.assertRaises(chatgpt_auth.AuthError) as caught:
                chatgpt_auth._finish_authorization(
                    job,
                    {"grant_type": "authorization_code"},
                    "oaiapp_test",
                    "nonce",
                    previous,
                )
        self.assertEqual(str(caught.exception), "Вход отменён")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertEqual(saved["access_token"], "old-access")
        self.assertEqual(saved["refresh_token"], "old-refresh")

    def test_cancel_during_token_check_does_not_save(self) -> None:
        self._session()
        job = chatgpt_auth.LoginJob()

        def fake_form(url, fields, *, job=None):
            return {
                "access_token": "leaked",
                "refresh_token": "leaked",
                "id_token": "id-token",
                "expires_in": 60,
                "scope": "openid",
            }

        def verify(token, audience, nonce):
            job.cancel()
            return {"email": "person@example.com", "sub": "subject-1"}

        with patch.object(chatgpt_auth, "_form", side_effect=fake_form), patch.object(
            chatgpt_auth, "verify_id_token", side_effect=verify,
        ):
            with self.assertRaises(chatgpt_auth.AuthError) as caught:
                chatgpt_auth._finish_authorization(
                    job,
                    {"grant_type": "authorization_code"},
                    "oaiapp_test",
                    "nonce",
                    chatgpt_auth.load_session(),
                )
        self.assertEqual(str(caught.exception), "Вход отменён")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertEqual(saved["access_token"], "old-access")

    def test_finish_authorization_stores_the_session(self) -> None:
        job = chatgpt_auth.LoginJob()
        seen: dict = {}

        def fake_form(url, fields, *, job=None):
            seen["job"] = job
            seen["fields"] = fields
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "id_token": "id-token",
                "expires_in": 3600,
                "scope": "openid email",
            }

        with patch.object(chatgpt_auth, "_form", side_effect=fake_form), patch.object(
            chatgpt_auth, "verify_id_token", return_value={"email": "person@example.com", "sub": "subject-1"},
        ):
            session = chatgpt_auth._finish_authorization(
                job,
                {"grant_type": "authorization_code", "code": "abc"},
                "oaiapp_test",
                "nonce",
                None,
            )
        self.assertIs(seen["job"], job)
        self.assertEqual(seen["fields"]["code"], "abc")
        self.assertEqual(session["access_token"], "new-access")
        self.assertEqual(session["email"], "person@example.com")
        saved = chatgpt_auth.load_session()
        assert saved is not None
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["client_id"], "oaiapp_test")

    def test_form_oserror_while_cancelled_is_cancel(self) -> None:
        job = chatgpt_auth.LoginJob()

        def fake_open(request, timeout, bound):
            bound.cancel()
            raise OSError("reset")

        with patch.object(chatgpt_auth, "_open_form", side_effect=fake_open):
            with self.assertRaises(chatgpt_auth.AuthError) as caught:
                chatgpt_auth._form(
                    chatgpt_auth.TOKEN_URL,
                    {"grant_type": "authorization_code"},
                    job=job,
                )
        self.assertEqual(str(caught.exception), "Вход отменён")

    def test_form_without_job_still_reports_a_network_error(self) -> None:
        def fake_open(request, timeout, job):
            self.assertIsNone(job)
            raise OSError("down")

        with patch.object(chatgpt_auth, "_open_form", side_effect=fake_open):
            with self.assertRaises(chatgpt_auth.AuthError) as caught:
                chatgpt_auth._form(chatgpt_auth.TOKEN_URL, {"grant_type": "refresh_token"})
        self.assertEqual(str(caught.exception), "Нет соединения с OpenAI")

    def test_cancel_closes_the_exchange_socket(self) -> None:
        class _Sock:
            def __init__(self) -> None:
                self.shut = False
                self.closed = False

            def shutdown(self, how) -> None:
                self.shut = True

            def close(self) -> None:
                self.closed = True

        job = chatgpt_auth.LoginJob()
        link = chatgpt_auth._LoginHTTPSConnection("auth.openai.com")
        link._job = job
        sock = _Sock()

        def fake_connect(self) -> None:
            self.sock = sock

        with patch.object(chatgpt_auth.http.client.HTTPSConnection, "connect", fake_connect):
            link.connect()
        self.assertIs(job._socket, sock)
        job.cancel()
        self.assertTrue(job.cancelled)
        self.assertTrue(sock.shut)
        self.assertTrue(sock.closed)
        self.assertIsNone(job._socket)

    def test_cancelled_invalid_grant_does_not_retry(self) -> None:
        calls: list[int] = []
        job = chatgpt_auth.LoginJob()

        def authorize(bound, saved):
            calls.append(1)
            bound.cancel()
            error = chatgpt_auth.AuthError("отклонён")
            error.code = "invalid_grant"
            raise error

        with patch.object(chatgpt_auth, "_authorize", side_effect=authorize):
            with self.assertRaises(chatgpt_auth.AuthError):
                chatgpt_auth._login(job)
        self.assertEqual(calls, [1])

    def test_model_list_survives_a_restart(self) -> None:
        self._session()
        session = chatgpt_auth.load_session()
        assert session is not None
        session["models"] = [{"slug": "gpt-test", "display_name": "GPT Test"}]
        session["model"] = "gpt-test"
        chatgpt_auth.save_session(session)
        chatgpt_auth._MODELS = []
        self.assertEqual(chatgpt_auth.cached_models()[0]["slug"], "gpt-test")
        self.assertEqual(chatgpt_auth.saved_model(), "gpt-test")

    def test_vendor_folder_is_added_for_imports(self) -> None:
        folder = Path(self.tmp.name)
        with patch.object(chatgpt_auth, "_vendor_dir", return_value=folder):
            chatgpt_auth.ensure_vendor_path()
        self.assertIn(str(folder), sys.path)
        sys.path.remove(str(folder))
        self.assertEqual(
            chatgpt_auth.auth_error_text("unknown_code", "Сервер вернул подробности"),
            "Сервер вернул подробности",
        )

    def test_claims_reject_expired_nonce_and_audience(self) -> None:
        claims = {
            "iss": chatgpt_auth.ISSUER,
            "aud": "oaiapp_test",
            "nonce": "n1",
            "exp": time.time() + 60,
        }
        chatgpt_auth.validate_claims(claims, "oaiapp_test", "n1", time.time())
        with self.assertRaises(chatgpt_auth.AuthError):
            chatgpt_auth.validate_claims(claims, "oaiapp_test", "other", time.time())
        with self.assertRaises(chatgpt_auth.AuthError):
            chatgpt_auth.validate_claims(claims, "other-app", "n1", time.time())
        with self.assertRaises(chatgpt_auth.AuthError):
            chatgpt_auth.validate_claims({**claims, "exp": time.time() - 5}, "oaiapp_test", "n1", time.time())


class ResponseRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in ("DWGREADER_CHATGPT_ACCESS_TOKEN", "DWGREADER_CHATGPT_MODEL", "LLM_API_KEY")
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_sse_waits_for_completed_event(self) -> None:
        lines = [line.decode("utf-8") for line in _sse('{"green":["Газон"]}')]
        self.assertEqual(_response_text_from_sse(lines), '{"green":["Газон"]}')
        with self.assertRaises(ChatGPTRouteError) as caught:
            _response_text_from_sse(["data: {\"type\":\"response.output_text.delta\",\"delta\":\"x\"}\n", "\n"])
        self.assertEqual(caught.exception.code, "stream_incomplete")
        failed = [
            'event: response.failed\n',
            'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}\n',
            "\n",
        ]
        with self.assertRaises(ChatGPTRouteError) as limited:
            _response_text_from_sse(failed)
        self.assertFalse(limited.exception.fallback)

    def test_chatgpt_route_does_not_wake_the_router(self) -> None:
        os.environ["DWGREADER_CHATGPT_ACCESS_TOKEN"] = "test-token"
        os.environ["DWGREADER_CHATGPT_MODEL"] = "gpt-test"
        box: dict = {}

        def fake_open(request, timeout=0):
            return _Stream(_sse(_GREEN), box, request)

        with patch.object(urllib.request, "urlopen", side_effect=fake_open), patch(
            "dxf_read.dxf._ensure_model_awake",
            side_effect=AssertionError("wake"),
        ):
            selection = select_target_layers(["Газон"])
        self.assertEqual(selection.model, "gpt-test")
        self.assertEqual(selection.by_category["green"], ["Газон"])
        self.assertTrue(box["body"]["instructions"])
        self.assertEqual(box["body"]["input"], [{"role": "user", "content": box["body"]["input"][0]["content"]}])
        self.assertFalse(box["body"]["store"])
        self.assertTrue(box["body"]["stream"])
        self.assertNotIn("temperature", box["body"])
        self.assertEqual(box["authorization"], "Bearer test-token")

    def test_without_oauth_the_api_route_stays(self) -> None:
        os.environ.pop("DWGREADER_CHATGPT_ACCESS_TOKEN", None)
        os.environ.pop("DWGREADER_CHATGPT_MODEL", None)

        def chat(**kwargs):
            self.assertEqual(kwargs["api_key"], "router-key")
            return _GREEN

        with patch("dxf_read.dxf._ensure_model_awake") as wake, patch(
            "dxf_read.dxf._request_chat_completion",
            side_effect=lambda **kwargs: chat(**kwargs),
        ), patch("dxf_read.dxf._request_responses", side_effect=AssertionError("responses")):
            selection = select_target_layers(["Газон"], api_key="router-key")
        wake.assert_called_once()
        self.assertEqual(selection.by_category["green"], ["Газон"])

    def test_revoked_session_does_not_switch_to_api(self) -> None:
        os.environ["DWGREADER_CHATGPT_ACCESS_TOKEN"] = "test-token"
        os.environ["DWGREADER_CHATGPT_MODEL"] = "gpt-test"

        def denied(*args, **kwargs):
            raise ChatGPTRouteError("Сессия ChatGPT недействительна", code="invalid_token", fallback=True)

        with patch("dxf_read.dxf._request_responses", side_effect=denied), patch(
            "dxf_read.dxf._ensure_model_awake",
        ) as wake, patch("dxf_read.dxf._request_chat_completion", side_effect=AssertionError("api")):
            with self.assertRaises(ChatGPTNeedChoice) as caught:
                select_target_layers(["Газон"], api_key="router-key")
        wake.assert_not_called()
        self.assertIn("недействительна", str(caught.exception))

    def test_forced_api_ignores_the_chatgpt_token(self) -> None:
        os.environ["DWGREADER_CHATGPT_ACCESS_TOKEN"] = "test-token"
        os.environ["DWGREADER_CHATGPT_MODEL"] = "gpt-test"
        os.environ["DWGREADER_FORCE_API"] = "1"

        def chat(**kwargs):
            self.assertEqual(kwargs["api_key"], "router-key")
            return _GREEN

        try:
            with patch("dxf_read.dxf._ensure_model_awake"), patch(
                "dxf_read.dxf._request_chat_completion",
                side_effect=lambda **kwargs: chat(**kwargs),
            ), patch("dxf_read.dxf._request_responses", side_effect=AssertionError("responses")):
                selection = select_target_layers(["Газон"], api_key="router-key")
        finally:
            os.environ.pop("DWGREADER_FORCE_API", None)
        self.assertNotEqual(selection.model, "gpt-test")
        self.assertEqual(selection.by_category["green"], ["Газон"])

    def test_plan_limit_does_not_use_the_api_key(self) -> None:
        os.environ["DWGREADER_CHATGPT_ACCESS_TOKEN"] = "test-token"
        os.environ["DWGREADER_CHATGPT_MODEL"] = "gpt-test"

        def limited(*args, **kwargs):
            raise ChatGPTRouteError("лимит", code="subscription_sharing_usage_limit_exceeded", fallback=False)

        with patch("dxf_read.dxf._request_responses", side_effect=limited), patch(
            "dxf_read.dxf._request_chat_completion",
            side_effect=AssertionError("api"),
        ):
            with self.assertRaises(ChatGPTNeedChoice) as caught:
                select_target_layers(["Газон"])
        self.assertIn("лимит", str(caught.exception))

    def test_api_key_comes_from_the_environment(self) -> None:
        os.environ["LLM_API_KEY"] = "from-env"
        with use_settings(PipelineSettings(llm_api_key="")):
            self.assertEqual(_resolved_api_key(None), "from-env")
        self.assertEqual(_resolved_api_key("explicit"), "explicit")

    def test_settings_payload_does_not_store_the_api_key(self) -> None:
        source = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay" / "__init__.py"
        text = source.read_text(encoding="utf-8")
        start = text.index("def _settings_payload")
        end = text.index("\ndef ", start + 1)
        self.assertNotIn("llm_api_key", text[start:end])

    def test_responses_request_can_be_read_back(self) -> None:
        box: dict = {}

        def fake_open(request, timeout=0):
            return _Stream(_sse(_GREEN), box, request)

        with patch.object(urllib.request, "urlopen", side_effect=fake_open):
            text = _request_responses_once("system", "layers", "gpt-test", "test-token", 5)
        self.assertEqual(text, _GREEN)
        self.assertEqual(set(box["body"]), {"model", "instructions", "input", "store", "stream"})
