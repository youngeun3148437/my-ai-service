"""확인된 프로필과 계정 동작을 하나의 카드로 표시한다."""
from html import escape

import streamlit as st

from ui.auth import account_tokens, load_profile, profile
from ui.common import base_url


def account(api):
    st.header("계정")
    if not st.session_state.ui_profile_attempted:
        load_profile(api)
    record = st.session_state.ui_profile
    if record is None:
        # 기존 조회 오류·첫 등록 절차를 유지한다. 조회 실패를 빈 프로필로 취급하지 않는다.
        profile(api)
        with st.container(horizontal=True, horizontal_alignment="right"):
            account_tokens(base_url())
        return
    if st.session_state.ui_profile_error:
        st.warning("최신 정보를 확인하지 못했다. 마지막으로 확인한 프로필을 표시한다.")
        if st.button("다시 불러오기", key="release_profile_retry"):
            load_profile(api)
            st.rerun()
    with st.container(key="release_account_card", border=True):
        avatar, identity = st.columns([1, 5], vertical_alignment="center")
        with avatar:
            # 사용자 입력을 HTML에 넣기 전에 escape해 이름을 태그로 해석하지 않게 한다.
            initial = escape(record["display_name"].strip()[:1] or "나")
            st.html(f'<div class="release-avatar" aria-hidden="true">{initial}</div>')
        with identity:
            st.caption("내 프로필")
            st.subheader(record["display_name"])
            role = st.session_state.get("ui_role", "user")
            st.badge("관리자" if role == "admin" else "일반 사용자", icon=":material/person:", color="blue")
        st.divider()
        with st.container(horizontal=True, horizontal_alignment="right"):
            account_tokens(base_url())
