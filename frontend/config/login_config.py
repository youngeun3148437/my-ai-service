"""브라우저 복귀 주소와 Streamlit 서버 전용 인증 설정을 읽는다."""

import os
from urllib.parse import urlsplit, urlunsplit

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from clients.api_client import ApiError


def setting(name: str, default=None):
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        return st.secrets.get(name, default)
    except StreamlitSecretNotFoundError as error:
        if error.error_id != 'no-secrets-found':
            raise
        return default


def app_public_url() -> str:
    # 로컬도 127.0.0.1과 localhost를 혼용하지 않는다. callback은 한 개의 고정 주소다.
    value = str(setting('APP_PUBLIC_URL', 'http://127.0.0.1:8501/'))
    try:
        parsed = urlsplit(value)
        port = parsed.port
        local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or port == 0
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or any(char in value for char in '? #\\*\';<>"')
                or any(char.isspace() for char in value) or (parsed.scheme != 'https' and not local)):
            raise ValueError()
    except ValueError:
        raise ApiError(503, 'LOGIN_CONFIG_ERROR', 'APP_PUBLIC_URL에 화면의 정확한 기본 주소를 설정한다.') from None
    return urlunsplit((parsed.scheme, parsed.netloc, '/', '', ''))


def login_client_secret() -> str:
    secret = setting('LOGIN_CLIENT_SECRET', '')
    if not isinstance(secret, str) or len(secret) < 32:
        raise ApiError(503, 'LOGIN_CONFIG_ERROR', '로그인 서버 연결 설정을 확인한다.')
    return secret
