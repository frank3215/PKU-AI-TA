import pytest
import requests
from unittest.mock import MagicMock, patch


class TestGetSession:
    def test_raises_without_credentials(self):
        from auth.iaaa import get_session
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = ""
            mock_settings.pku_password = ""
            with pytest.raises(RuntimeError, match="PKU_USERNAME"):
                get_session()

    def test_raises_on_iaaa_failure(self):
        from auth.iaaa import get_session
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = "user"
            mock_settings.pku_password = "pass"

            with patch("auth.iaaa.requests.Session") as MockSession:
                mock_session = MagicMock()
                MockSession.return_value = mock_session

                key_resp = MagicMock()
                key_resp.raise_for_status = MagicMock()
                key_resp.json.return_value = {
                    "success": True,
                    "key": (
                        "-----BEGIN PUBLIC KEY-----\n"
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqw9PsMk8v9ED/LiLT62I"
                        "DnelyIA/s8blyxqNmbgXT4xtq+Y64Bd+THYPZ4dUIRuFmMvPowQm9wL27W3PEtQy"
                        "C8VN+TzW/nPzc74fy9cRxgaSh1FXNQBqYZtltb6G5YvwBvZlYdKhE3Oo3noUD0FJ"
                        "JC11Nmcy2/x1V2pwXHRy2DHKaWB1EEtQ9dRxuMZolZIpEwWnT4CHfwEvth83kNRp"
                        "E8471KJEqyQqmqJt3JRerH4X4p41zQFIxCsrznAwku3b1qm0vgGLQ8t7XEiCjDX0"
                        "m5yIJEuW5t1YcteutuJX5+5oXxe2Fo04Wkn1pO6+QoJopqHcHJD5C+7GlnPOLB1c"
                        "DQIDAQAB\n"
                        "-----END PUBLIC KEY-----"
                    ),
                }

                fail_resp = MagicMock()
                fail_resp.raise_for_status = MagicMock()
                fail_resp.json.return_value = {"success": False, "errors": "Invalid password"}

                mock_session.get.return_value = key_resp
                mock_session.post.return_value = fail_resp

                with pytest.raises(RuntimeError, match="IAAA login failed"):
                    get_session()

    def test_returns_client_on_success(self):
        from auth.iaaa import get_session
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = "user"
            mock_settings.pku_password = "pass"

            with patch("auth.iaaa.requests.Session") as MockSession:
                mock_session = MagicMock()
                MockSession.return_value = mock_session

                key_resp = MagicMock()
                key_resp.raise_for_status = MagicMock()
                # Must set json as a callable that returns the dict (not an attribute alias)
                key_resp.json = MagicMock(return_value={
                    "success": True,
                    "key": (
                        "-----BEGIN PUBLIC KEY-----\n"
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqw9PsMk8v9ED/LiLT62I"
                        "DnelyIA/s8blyxqNmbgXT4xtq+Y64Bd+THYPZ4dUIRuFmMvPowQm9wL27W3PEtQy"
                        "C8VN+TzW/nPzc74fy9cRxgaSh1FXNQBqYZtltb6G5YvwBvZlYdKhE3Oo3noUD0FJ"
                        "JC11Nmcy2/x1V2pwXHRy2DHKaWB1EEtQ9dRxuMZolZIpEwWnT4CHfwEvth83kNRp"
                        "E8471KJEqyQqmqJt3JRerH4X4p41zQFIxCsrznAwku3b1qm0vgGLQ8t7XEiCjDX0"
                        "m5yIJEuW5t1YcteutuJX5+5oXxe2Fo04Wkn1pO6+QoJopqHcHJD5C+7GlnPOLB1c"
                        "DQIDAQAB\n"
                        "-----END PUBLIC KEY-----"
                    ),
                })

                token_resp = MagicMock()
                token_resp.raise_for_status = MagicMock()
                token_resp.json.return_value = {"success": True, "token": "abc123"}

                # Simulate campusLogin returning a redirect (302) with Set-Cookie
                redirect_resp = MagicMock()
                redirect_resp.status_code = 302
                redirect_resp.raise_for_status = MagicMock()
                redirect_resp.headers = {"location": "http://course.pku.edu.cn/somepage"}
                redirect_resp.raw.headers.getlist = MagicMock(return_value=[
                    "s_session_id=TEST_SESSION_ID; Path=/; HttpOnly",
                ])

                # Second response after redirect
                final_resp = MagicMock()
                final_resp.status_code = 200
                final_resp.raise_for_status = MagicMock()
                final_resp.raw.headers.getlist = MagicMock(return_value=[
                    "s_session_id=TEST_SESSION_ID; Path=/; HttpOnly",
                ])

                # Configure session mock:
                # - iaaa_session.get(...) uses mock_session.get.side_effect
                # - iaaa_session.post(...) uses mock_session.post.return_value
                # - _follow's bb_session.request(...) uses mock_session.request.side_effect
                mock_session.get.side_effect = [
                    key_resp,      # 1: GET public key via iaaa_session
                    redirect_resp,  # 2: GET campusLogin (302) via bb_session
                    final_resp,     # 3: GET redirect location (200) via bb_session
                ]
                mock_session.post.return_value = token_resp  # POST oauthlogin
                mock_session.request.side_effect = [
                    redirect_resp,  # 1: GET campusLogin (302) via _follow
                    final_resp,     # 2: GET redirect location (200) via _follow
                ]

                result = get_session()

                assert result is not None
                # Verify oauthlogin was called (token 'abc123' ends up in campusLogin params)
                request_calls = [c for c in mock_session.request.call_args_list]
                assert any("abc123" in str(c) for c in request_calls)
