"""대화 이동·전송은 최신으로, 이전 기록 읽기는 현재 위치를 유지한다."""
from uuid import uuid4

import streamlit as st


JS = r"""
let view = null;
let active = null;
export default function(component) {
    active?.();
    const {data, parentElement} = component;
    const doc = parentElement.ownerDocument || document;
    const main = doc.querySelector('[data-testid="stMain"]') || doc.scrollingElement;
    if (!main) return;
    const identity = `${data.scope}:${data.conversation_id}`;
    const changed = view?.identity !== identity;
    if (changed) view = {identity, latest: data.latest, preserve: data.preserve,
                         follow: true, anchor: null, adjusting: false, userUntil: 0};
    const state = view;
    // 새 대화와 명시적 전송만 강제로 이동한다. 일반 재실행은 읽던 위치를 유지한다.
    if (!changed && state.latest !== data.latest) {
        state.follow = true;
        state.anchor = null;
    } else if (!changed && state.preserve !== data.preserve) {
        state.follow = false;
    }
    state.latest = data.latest;
    state.preserve = data.preserve;
    let frame = 0, releaseFrame = 0, stopped = false;
    const nearBottom = () => main.scrollHeight - main.clientHeight - main.scrollTop < 96;
    const rows = () => [...main.querySelectorAll('[class*="st-key-release_message_row_"]')];
    const rowKey = row => [...row.classList].find(name => /^st-key-release_message_row_\d+$/.test(name));
    const offset = row => row.getBoundingClientRect().top - main.getBoundingClientRect().top;
    const remember = () => {
        const bounds = main.getBoundingClientRect();
        const row = rows().find(item => {
            const rect = item.getBoundingClientRect();
            return rect.bottom > bounds.top && rect.top < bounds.bottom;
        });
        state.anchor = row ? {key: rowKey(row), top: offset(row)} : null;
    };
    const latestButton = doc.createElement('button');
    latestButton.type = 'button';
    latestButton.textContent = '↓ 최신 메시지로';
    latestButton.setAttribute('aria-label', '최신 메시지로 이동');
    latestButton.style.cssText = 'position:fixed;right:28px;bottom:24px;z-index:20;' +
        'border:1px solid rgba(128,128,128,.2);border-radius:24px;padding:9px 16px;' +
        'background:#2866aa;color:white;font:inherit;font-size:14px;cursor:pointer;' +
        'box-shadow:0 3px 16px rgba(20,40,70,.15);';
    const showButton = () => { latestButton.hidden = state.follow || nearBottom(); };
    const layout = () => {
        frame = 0;
        if (stopped) return;
        const surface = main.querySelector(`.st-key-release_chat_surface_${data.conversation_id}`);
        // 입력까지 도착한 레이아웃에서 위치를 잡는다. 중간 높이를 먼저 노출하지 않는다.
        if (surface && !surface.querySelector('.st-key-release_composer')) return;
        state.adjusting = true;
        if (state.follow) {
            main.scrollTop = Math.max(0, main.scrollHeight - main.clientHeight);
        } else if (state.anchor) {
            // 이전 기록을 앞에 붙여도 같은 메시지가 화면의 같은 높이에 머무른다.
            const anchor = rows().find(row => rowKey(row) === state.anchor.key);
            if (anchor) main.scrollTop += offset(anchor) - state.anchor.top;
        }
        remember();
        showButton();
        if (surface) surface.style.visibility = 'visible';
        cancelAnimationFrame(releaseFrame);
        releaseFrame = requestAnimationFrame(() => { state.adjusting = false; });
    };
    const schedule = () => { if (!frame && !stopped) frame = requestAnimationFrame(layout); };
    const input = () => { state.userUntil = Date.now() + 1200; };
    const onScroll = () => {
        if (state.adjusting) return;
        // 스트리밍으로 생긴 자동 레이아웃 변화와 사용자의 스크롤을 구분한다.
        if (Date.now() < state.userUntil) state.follow = nearBottom();
        else if (nearBottom()) state.follow = true;
        remember();
        showButton();
    };
    const wheel = event => {
        input();
        if (event.deltaY < 0) { state.follow = false; remember(); showButton(); }
    };
    const pointer = event => { if (event.target === main) input(); };
    const keyboard = event => {
        if (event.target?.closest?.('textarea, input, [contenteditable="true"]')) return;
        if (['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '].includes(event.key)) {
            input();
            if (['PageUp', 'Home', 'ArrowUp'].includes(event.key)) state.follow = false;
        }
    };
    const click = event => {
        if (event.target?.closest?.('button')?.textContent.trim() === '이전 기록 더 보기') {
            // 서버 조회가 시작되기 전에 기준 메시지의 위치를 기억한다.
            state.follow = false;
            remember();
            showButton();
        }
    };
    latestButton.addEventListener('click', () => {
        state.follow = true;
        state.anchor = null;
        schedule();
    });
    parentElement.appendChild(latestButton);
    main.addEventListener('scroll', onScroll, {passive: true});
    main.addEventListener('wheel', wheel, {passive: true});
    main.addEventListener('touchstart', input, {passive: true});
    main.addEventListener('pointerdown', pointer, {passive: true});
    main.addEventListener('keydown', keyboard);
    main.addEventListener('click', click);
    const mutation = new MutationObserver(schedule);
    mutation.observe(main, {subtree: true, childList: true, characterData: true});
    const resize = new ResizeObserver(schedule);
    resize.observe(main.querySelector('[data-testid="stMainBlockContainer"]') || main);
    schedule();
    const cleanup = () => {
        if (stopped) return;
        stopped = true;
        mutation.disconnect();
        resize.disconnect();
        cancelAnimationFrame(frame);
        cancelAnimationFrame(releaseFrame);
        state.adjusting = false;
        main.removeEventListener('scroll', onScroll);
        main.removeEventListener('wheel', wheel);
        main.removeEventListener('touchstart', input);
        main.removeEventListener('pointerdown', pointer);
        main.removeEventListener('keydown', keyboard);
        main.removeEventListener('click', click);
        latestButton.remove();
        if (active === cleanup) active = null;
    };
    active = cleanup;
    return cleanup;
}
"""

_scroll = st.components.v2.component("course_chat_scroll", js=JS, isolate_styles=False)


def request_latest(conversation_id: int):
    # 완료 응답 뒤 재실행에서 한 번 이동한다. AI 조각마다 새 요청을 만들지 않는다.
    st.session_state["ui_chat_scroll_latest"] = (conversation_id, uuid4().hex)


def preserve_history(conversation_id: int):
    st.session_state["ui_chat_scroll_preserve"] = (conversation_id, uuid4().hex)


def prepare_chat_layout(conversation_id: int):
    # 높이는 유지한 채 최초 위치만 준비한다. JS 지연·실패에도 1.5초 뒤 내용은 보인다.
    st.html(f'''<style>
    @keyframes release-chat-reveal {{ to {{ visibility: visible; }} }}
    .st-key-release_chat_surface_{conversation_id} {{
        visibility: hidden; animation: release-chat-reveal 0s 1.5s forwards;
    }}
    </style>''')


def chat_scroll(conversation_id: int):
    # 계정을 바꾸면 clear_account가 ui_ 상태를 비우므로 이전 계정의 위치를 재사용하지 않는다.
    if "ui_chat_scroll_scope" not in st.session_state:
        st.session_state["ui_chat_scroll_scope"] = uuid4().hex
    latest = st.session_state.get("ui_chat_scroll_latest", (None, None))
    preserve = st.session_state.get("ui_chat_scroll_preserve", (None, None))
    _scroll(data={"scope": st.session_state["ui_chat_scroll_scope"], "conversation_id": conversation_id,
                  "latest": latest[1] if latest[0] == conversation_id else None,
                  "preserve": preserve[1] if preserve[0] == conversation_id else None},
            key="release_chat_scroll")
