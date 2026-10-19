"""브라우저 인증 이동과 짧은 PKCE 거래만 연결한다. 로그인 쿠키는 읽지 않는다."""

from types import SimpleNamespace

import streamlit as st


JS = r"""
const watchers = new WeakMap();
// 창 참조는 메모리에만 둔다. 쿠키·세션 ID·JWT는 이 컴포넌트가 받지 않는다.
const popups = new Map();
const transactionKey = 'course_browser_auth_transaction_v2';
const callbackKey = 'course_browser_auth_callback_v2';
// 복귀 창의 표시는 현재 문서에만 둔다. 새 탭·새로고침이 완료 창을 물려받지 않는다.
let relay = null;
const signalKey = 'course_browser_auth_changed_v2';
const channelName = 'course_browser_auth_return_v3';
const maxAge = 5 * 60 * 1000;
const pattern = /^[A-Za-z0-9_-]{43}$/;
export default function(component) {
    const {data, parentElement, setStateValue} = component;
    watchers.get(parentElement)?.();
    parentElement.replaceChildren();
    let stopped = false, channel = null, timer = null, reported = data.known_receipt;
    const framed = window.self !== window.top;
    const report = (payload) => {
        if (stopped) return;
        const receipt = {nonce: data.nonce, ok: true, framed, ...payload};
        if (JSON.stringify(receipt) === JSON.stringify(reported)) return;
        reported = receipt;
        setStateValue('receipt', receipt);
    };
    const read = key => {
        const value = sessionStorage.getItem(key);
        return value ? JSON.parse(value) : null;
    };
    const fresh = value => value && Number.isFinite(value.created)
        && Date.now() - value.created >= -5000 && Date.now() - value.created <= maxAge;
    const clear = () => {
        sessionStorage.removeItem(transactionKey);
        sessionStorage.removeItem(callbackKey);
    };
    const random = () => {
        const bytes = crypto.getRandomValues(new Uint8Array(32));
        return btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
    };
    const challenge = async verifier => {
        const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
        return btoa(String.fromCharCode(...new Uint8Array(hash))).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
    };
    const notify = kind => {
        try { localStorage.setItem(signalKey, JSON.stringify({kind, id: random()})); } catch (_) {}
    };
    const changed = event => {
        if (event.key !== signalKey || !event.newValue) return;
        try {
            const signal = JSON.parse(event.newValue);
            if (['login', 'logout'].includes(signal.kind) && pattern.test(signal.id))
                report({kind: 'account_changed', change: signal.kind, signal_id: signal.id});
        } catch (_) {}
    };
    const valid = (message, transaction) => fresh(transaction)
        && transaction.redirect_uri === data.redirect_uri && message.redirect_uri === data.redirect_uri
        && pattern.test(message.state || '') && transaction.state === message.state
        && Boolean(message.code) !== Boolean(message.error)
        && (!message.code || (pattern.test(message.code) && transaction.kind === 'login'))
        && (!message.error || ['cancelled', 'logged_out'].includes(message.error))
        && (message.error !== 'logged_out' || transaction.kind === 'logout');
    const accept = (message, transaction) => {
        if (!valid(message, transaction)) return false;
        const callback = {kind: 'callback', state: message.state, code: message.code, error: message.error,
            code_verifier: message.code ? transaction.verifier : null,
            redirect_uri: transaction.redirect_uri, reason: transaction.reason, created: transaction.created};
        sessionStorage.setItem(callbackKey, JSON.stringify(callback));
        report(callback);
        return true;
    };
    const send = message => channel?.postMessage(message);
    const button = (label, action) => {
        parentElement.style.display = 'flex';
        parentElement.style.justifyContent = 'flex-end';
        const element = document.createElement('button');
        element.type = 'button';
        element.textContent = label;
        element.style.cssText = 'display:inline-block;text-decoration:none;font:inherit;font-size:14px;padding:.5rem .9rem;border-radius:.5rem;cursor:pointer;' +
            (data.primary ? 'border:1px solid #2866aa;background:#2866aa;color:white;' : 'border:1px solid #b9bdc5;background:transparent;color:inherit;');
        element.addEventListener('click', event => {
            try { action(); }
            catch (_) { event.preventDefault(); report({kind: 'popup_blocked'}); }
        });
        parentElement.appendChild(element);
        return element;
    };
    const openPopup = (transaction, url = 'about:blank') => {
        const existing = popups.get(transaction.state);
        if (existing && !existing.closed) { existing.focus(); return existing; }
        // await·Python 왕복보다 먼저 실제 클릭 안에서 연다. Cloud가 허용한 새 창만 사용한다.
        const popup = window.open(url, '_blank');
        if (!popup) return null;
        try {
            // 새 창이 복사한 거래를 제거한다. verifier는 원래 화면에만 남긴다.
            popup.sessionStorage.removeItem(transactionKey);
            popup.sessionStorage.removeItem(callbackKey);
            popup.opener = null;
        } catch (_) { popup.close(); return null; }
        popups.set(transaction.state, popup);
        return popup;
    };
    const action = data.op === 'authorize_button' || data.op === 'logout_button';
    if (!action) addEventListener('storage', changed);
    const stop = () => {
        if (stopped) return;
        stopped = true; removeEventListener('storage', changed);
        if (timer) clearInterval(timer);
        channel?.close();
        parentElement.replaceChildren();
    };
    watchers.set(parentElement, stop);
    (async () => {
        try {
            channel = new BroadcastChannel(channelName);
            // 이전 버전의 복귀 표시는 새 탭의 일반 실행을 가로막을 수 있어 지운다.
            sessionStorage.removeItem('course_browser_auth_relay_v3');
            sessionStorage.removeItem('course_browser_auth_relay_done_v3');
            const url = new URL(location.href);
            const keys = ['login_code', 'login_state', 'login_error'];
            const returning = keys.some(key => url.searchParams.has(key));
            if (!returning && relay?.completed) {
                report({kind: 'relay_complete'});
                return;
            }
            if (returning || relay) {
                const message = returning ? {type: 'callback', state: url.searchParams.get('login_state'),
                    code: url.searchParams.get('login_code'), error: url.searchParams.get('login_error'),
                    redirect_uri: data.redirect_uri, created: Date.now()} : relay;
                const single = keys.every(key => url.searchParams.getAll(key).length <= 1);
                for (const key of keys) url.searchParams.delete(key);
                history.replaceState(history.state, '', url.pathname + url.search + url.hash);
                const transaction = read(transactionKey);
                if (returning && transaction) {
                    if (!single || !accept(message, transaction)) {
                        clear(); report({kind: 'error', error: 'invalid_callback'});
                    }
                    return;
                }
                // 새 창은 code·state만 원래 화면에 중계한다. 이 창에서는 서버 교환을 하지 않는다.
                if (!single || !fresh(message) || message.redirect_uri !== data.redirect_uri
                    || !pattern.test(message.state || '') || Boolean(message.code) === Boolean(message.error)
                    || (message.code && !pattern.test(message.code))
                    || (message.error && !['cancelled', 'logged_out'].includes(message.error))) {
                    relay = null;
                    report({kind: 'error', error: 'invalid_callback'}); return;
                }
                relay = message;
                channel.onmessage = event => {
                    if (event.data?.type !== 'ack' || event.data.state !== message.state) return;
                    clearInterval(timer);
                    relay = {completed: true};
                    report({kind: 'relay_complete'});
                    // 창 닫기는 이 프레임이 아닌, 그 창을 연 원래 화면이 수행한다.
                };
                let attempts = 0;
                timer = setInterval(() => {
                    if (++attempts > 90) {
                        clearInterval(timer); report({kind: 'relay_timeout'}); return;
                    }
                    send(message);
                }, 500);
                send(message);
                report({kind: 'relay_wait'});
                return;
            }
            if (!action) channel.onmessage = event => {
                const message = event.data;
                if (message?.type !== 'callback') return;
                const transaction = read(transactionKey);
                if (transaction?.popup) accept(message, transaction);
            };
            if (data.op === 'ack') {
                const pending = read(callbackKey), transaction = read(transactionKey);
                // 이전 로그아웃 ACK가 새 로그인 결과를 none으로 덮어쓰지 않는다.
                if (pending && pending.state !== data.callback_state && fresh(pending)) {
                    if (accept(pending, transaction)) return;
                }
                if (pending?.state === data.callback_state || transaction?.state === data.callback_state) {
                    send({type: 'ack', state: data.callback_state});
                    // Cloud 복귀 프레임의 top.close 대신 자신이 연 창 참조를 닫는다.
                    const popup = popups.get(data.callback_state);
                    if (popup && !popup.closed) popup.close();
                    clear(); popups.delete(data.callback_state);
                    if (data.broadcast) notify(data.broadcast);
                }
                report({kind: 'none'}); return;
            }
            if (action || data.op === 'authorize' || data.op === 'prepare_logout') {
                const login = ['authorize', 'authorize_button'].includes(data.op);
                let transaction = read(transactionKey);
                if (!fresh(transaction) || transaction.operation !== data.nonce) {
                    transaction = {state: random(), verifier: login ? random() : null, created: Date.now(),
                        redirect_uri: data.redirect_uri, kind: login ? 'login' : 'logout',
                        reason: data.reason || 'login', operation: data.nonce, popup: framed};
                    if (!action) { clear(); sessionStorage.setItem(transactionKey, JSON.stringify(transaction)); }
                }
                let target;
                if (login) {
                    target = new URL(data.authorize_url);
                    target.searchParams.set('redirect_uri', transaction.redirect_uri);
                    target.searchParams.set('state', transaction.state);
                    target.searchParams.set('code_challenge', await challenge(transaction.verifier));
                }
                if (stopped) return;
                const launch = () => {
                    // 화면을 오래 열어 뒀어도 실제 클릭부터 짧은 거래 수명을 계산한다.
                    transaction.created = Date.now();
                    clear(); sessionStorage.setItem(transactionKey, JSON.stringify(transaction));
                    const popup = framed ? openPopup(transaction, login ? target.href : undefined) : null;
                    if (framed && !popup) {
                        report({kind: 'popup_blocked'}); return;
                    }
                    if (login) {
                        if (!popup) location.assign(target.href);
                        report({kind: 'waiting'});
                    } else report({kind: 'logout_ready', state: transaction.state, redirect_uri: transaction.redirect_uri});
                };
                if (action || framed) {
                    button(data.label || (login ? '계속하기 · 로그인' : '로그아웃'), launch);
                    if (!action) report({kind: 'gesture_required'});
                } else if (login) { report({kind: 'redirecting'}); location.assign(target.href); }
                else launch();
                return;
            }
            if (data.op === 'logout') {
                const transaction = read(transactionKey);
                if (!fresh(transaction) || transaction.kind !== 'logout') throw Error('expired logout');
                const navigateLogout = popup => {
                    // 수신 확인으로 다시 렌더돼도 일회용 티켓 주소로 두 번 이동하지 않는다.
                    transaction.logout_navigated = true;
                    sessionStorage.setItem(transactionKey, JSON.stringify(transaction));
                    if (popup) popup.location.replace(data.logout_url);
                    else location.assign(data.logout_url);
                };
                if (transaction.popup) {
                    const popup = popups.get(transaction.state);
                    if (popup && !popup.closed) {
                        if (!transaction.logout_navigated) navigateLogout(popup);
                    }
                    else button('로그아웃 완료하기', () => {
                        const reopened = openPopup(transaction);
                        if (reopened) navigateLogout(reopened);
                        else report({kind: 'popup_blocked'});
                    });
                    report({kind: 'logout_pending'});
                } else {
                    report({kind: 'redirecting'});
                    if (!transaction.logout_navigated) navigateLogout(null);
                }
                return;
            }
            const callback = read(callbackKey);
            if (callback) {
                if (!fresh(callback)) { clear(); report({kind: 'error', error: 'expired_callback'}); }
                else report(callback);
                return;
            }
            const transaction = read(transactionKey);
            if (transaction?.popup && fresh(transaction)) { report({kind: 'waiting'}); return; }
            if (transaction) { clear(); report({kind: 'error', error: 'interrupted_auth'}); return; }
            report({kind: 'none'});
        } catch (_) {
            report({kind: 'error', error: 'browser_auth_unavailable', ok: false});
        }
    })();
    return () => {
        stop();
        if (watchers.get(parentElement) === stop) watchers.delete(parentElement);
    };
}
"""

_bridge = st.components.v2.component('course_browser_auth', js=JS, isolate_styles=False)


def browser_auth(operation: dict, *, slot="bridge"):
    # nonce마다 결과를 구분한다. 재등록 중에는 이미 받은 확인을 유지한다.
    key = 'login_auth_' + slot + '_' + operation['nonce']
    state = st.session_state
    state['lr_' + slot + '_nonce'] = operation['nonce']

    def matching(receipt):
        return isinstance(receipt, dict) and receipt.get('nonce') == operation['nonce']

    def remember(receipt):
        if state.get('lr_' + slot + '_nonce') == operation['nonce'] and matching(receipt):
            state['lr_' + slot + '_receipt'] = dict(receipt)

    def receive():
        remember(state.get(key, {}).get('receipt'))

    receipt = state.get('lr_' + slot + '_receipt')
    known = receipt if matching(receipt) else None
    result = _bridge(data={**operation, 'known_receipt': known}, key=key,
                     default={'receipt': None}, on_receipt_change=receive)
    remember(getattr(result, 'receipt', None))
    receipt = state.get('lr_' + slot + '_receipt')
    return SimpleNamespace(receipt=receipt if matching(receipt) else None)
