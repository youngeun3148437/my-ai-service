"""로그인·회원가입·프로필·로그아웃을 계정 화면으로 묶는다."""
import streamlit as st
from clients.api_client import ApiError
from ui.common import call, current_api, base_url as api_base_url
from state.login_session_state import auth_button, logout_persistent


def login(base_url: str):
    # 비밀번호 입력·HttpOnly 쿠키 발급은 브라우저가 방문하는 FastAPI 인증 화면에서 처리한다.
    with st.container(key="ui_auth_card"):
        st.header("로그인")
        st.caption("계정으로 로그인하고 대화를 이어 간다.")
        if st.session_state.get('lr_browser_framed'):
            st.caption('인증 창에서 로그인한다. 로그인 기록이 있으면 비밀번호 없이 이어 간다.')
        error = st.session_state.get('lr_auth_error')
        if error:
            st.warning(error)
        with st.container(horizontal=True, horizontal_alignment="right"):
            auth_button(base_url)


def signup(base_url: str):
    # 가입도 같은 인증 화면에서 진행한다. 가입 응답 자체를 로그인 상태로 저장하지 않는다.
    login(base_url)


def logout_action():
    # 서버의 복원 세션도 폐기해 새로고침·다른 탭에서 이전 계정이 복원되지 않게 한다.
    logout_persistent(api_base_url(), st.session_state)


def account_tokens(base_url: str):
    # 계정 페이지의 로그아웃 버튼을 서버 요청과 연결한다.
    auth_button(base_url, logout=True)


def profile_logout():
    # 프로필 확인·등록 화면의 로그아웃 버튼을 같은 동작에 연결한다.
    with st.container(key="ui_profile_logout_actions", horizontal=True, horizontal_alignment="right"):
        auth_button(api_base_url(), logout=True)


def load_profile(api):
    # GET /me 결과를 프로필 있음·확인된 부재·조회 오류 상태로 나눈다.
    # attempted는 조회 시도 여부다. 성공 여부는 아래의 status로 따로 기록한다.
    st.session_state.ui_profile_attempted = True
    try:
        result = api.request("GET", "/me")
        if not isinstance(result.get("display_name"), str):
            raise ApiError(200, "RESPONSE_ERROR", "계정 정보를 확인하지 못했다.")
    except ApiError as error:
        # 이 API의 프로필 부재 응답만 등록 화면으로 연결한다. 연결·권한 오류는 부재가 아니다.
        missing = error.status == 404 and error.code == "NOT_FOUND" and error.message == "프로필 기록을 찾을 수 없다"
        st.session_state.ui_profile_missing = missing
        if missing:
            st.session_state.ui_profile = None
        st.session_state.ui_profile_status = "missing" if missing else "error"
        st.session_state.ui_profile_error = "" if missing else error.message
        # 인증이 유효하지 않으면 토큰을 비우고 로그인 화면으로 돌아간다.
        if error.status == 401:
            st.session_state.ui_tokens = None
            st.session_state.ui_auth_mode = "login"
            st.session_state.ui_notice = "다시 로그인한다. 같은 계정으로 로그인하면 입력을 이어 쓸 수 있다."
            st.rerun()
        return False
    # 성공한 프로필과 준비 완료(ready) 상태를 보관해 다음 화면에서 사용한다.
    st.session_state.ui_profile = result
    st.session_state.ui_profile_missing = False
    st.session_state.ui_profile_status = "ready"
    st.session_state.ui_profile_error = ""
    return True


def profile(api, *, gate=False):
    # 조회 상태에 맞춰 표시 이름·재조회 안내·첫 등록 폼을 보여 준다.
    # gate=True는 선택한 페이지 본문에 앞서 프로필 확인·등록을 보여 주는 모드다.
    with st.container(key="ui_auth_card") if gate else st.container():
        if gate:
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                st.header("프로필 등록" if st.session_state.ui_profile_missing else "계정 정보 확인")
                auth_button(api_base_url(), logout=True)
        if not gate:
            st.subheader("내 정보")
        if not st.session_state.ui_profile_attempted:
            load_profile(api)
        if st.session_state.ui_profile_error:
            st.error(st.session_state.ui_profile_error)
        if st.session_state.ui_profile_status == "error":
            if st.button("다시 불러오기", key="ui_check_profile"):
                load_profile(api)
                st.rerun()
        if st.session_state.ui_profile:
            st.caption("표시 이름")
            st.text(st.session_state.ui_profile["display_name"])
            return
        # 프로필 부재가 확인되지 않았으면 등록 요청을 허용하지 않는다.
        if not st.session_state.ui_profile_missing:
            st.caption("계정 정보를 불러오지 못했다. 다시 확인한다.")
            return
        with st.container(key="ui_auth_card") if not gate else st.container():
            st.caption("대화에 사용할 표시 이름을 등록한다.")
            # 로그아웃은 실제 클릭에서 인증 창을 열어야 하므로 등록 form과 분리한다.
            with st.form("profile", border=False):
                name = st.text_input("표시 이름", value=st.session_state.ui_profile_name, max_chars=40, key="ui_name")
                if gate:
                    with st.container(horizontal=True, horizontal_alignment="right"):
                        submitted = st.form_submit_button("프로필 등록", type="primary", width="content")
                else:
                    with st.container(horizontal=True, horizontal_alignment="right"):
                        submitted = st.form_submit_button("프로필 등록", type="primary", width="content")
            if submitted:
                if not name.strip():
                    st.warning("표시 이름을 입력한다.")
                    return
                # 표시 이름을 등록하고 성공 응답을 받으면 준비 완료 상태로 바꾼다.
                result = call(api, "POST", "/me/profile", body={"display_name": name.strip()})
                if result is not None and isinstance(result.get("display_name"), str):
                    st.session_state.ui_profile = result
                    st.session_state.ui_profile_missing = False
                    st.session_state.ui_profile_status = "ready"
                    st.session_state.ui_profile_error = ""
                    st.rerun()
                error = st.session_state.ui_last_error
                # 등록 요청이 409이면 GET으로 현재 프로필을 확인한다.
                if error and error.status == 409:
                    if load_profile(api):
                        st.rerun()


def ensure_profile(api, *, account_page=False) -> bool:
    # 페이지를 열 수 있으면 True, 확인·등록 화면이 먼저 필요하면 False를 반환한다.
    if not st.session_state.ui_profile_attempted:
        load_profile(api)
    if account_page and st.session_state.ui_active_page == "account-settings":
        return True  # 계정 화면에서 시작한 등록·재조회 위젯은 같은 페이지에서 계속 처리한다.
    if st.session_state.ui_profile_status == "ready":
        return True
    if st.session_state.ui_profile_status == "error" and st.session_state.ui_profile and st.session_state.ui_active_page is not None:
        return True  # 확인한 표시 정보는 일시 조회 실패에도 유지한다. 새 로그인에서는 이 분기를 쓰지 않는다.
    # 준비가 필요하면 확인·등록 화면을 먼저 보여 주고 페이지 본문은 대기시킨다.
    profile(api, gate=True)
    return False
