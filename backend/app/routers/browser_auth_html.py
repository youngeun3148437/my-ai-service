"""API 도메인에서만 열리는 인증 카드를 만든다. 비밀번호는 HTML에 다시 넣지 않는다."""

from html import escape

from fastapi.responses import HTMLResponse

STYLE = '''
*{box-sizing:border-box}body{margin:0;background:#f6f8fb;color:#20334b;font-family:system-ui,"Malgun Gothic",sans-serif}
main{width:min(440px,calc(100% - 32px));margin:10vh auto;padding:34px;background:white;border:1px solid #e1e7ef;border-radius:22px;box-shadow:0 14px 45px #20334b09}
h1{font-size:26px;margin:0 0 10px}p{font-size:14px;color:#607086;line-height:1.6}.notice{padding:12px;border-radius:10px;background:#eef4ff;color:#294e7a}
label{display:block;font-size:14px;margin:20px 0 7px}input{width:100%;padding:12px;border:1px solid #ccd6e3;border-radius:10px;font:inherit}
.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:26px;align-items:center}button{border:0;border-radius:10px;padding:11px 17px;background:#eaf0f6;color:#30465f;font:inherit;cursor:pointer}
.primary{background:#2869b1;color:white}.link{background:none;padding:0;color:#2869b1;font-size:14px}.footer{display:flex;justify-content:space-between;align-items:center;margin-top:26px}
'''


def response(document: str, *, redirect_uri: str, nonce: str | None = None) -> HTMLResponse:
    # 외부 스크립트·프레임 삽입을 막고 인증 응답·복귀 주소를 캐시에 남기지 않는다.
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return HTMLResponse(document, headers={
        # 동일 출처 폼 POST의 Origin은 유지하고 외부 화면으로는 Referer를 보내지 않는다.
        'Cache-Control': 'private, no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'same-origin',
        'X-Content-Type-Options': 'nosniff',
        # Chrome은 폼 제출 뒤 303의 목적지도 검사한다. 서버가 허용한 정확한 복귀 주소만 더한다.
        'Content-Security-Policy': f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
                                  f"form-action 'self' {redirect_uri}; base-uri 'none'; frame-ancestors 'none'",
    })


def auth_card(flow_id: str, csrf: str, *, redirect_uri: str, signup=False, email='', message='') -> HTMLResponse:
    heading = '회원가입' if signup else '로그인'
    explanation = ('가입 후 이메일 확인을 마친 뒤 로그인한다.' if signup else '이메일과 비밀번호를 입력한다.')
    notice = f'<p class="notice" role="status">{escape(message)}</p>' if message else ''
    confirm = ('<label for="confirm">비밀번호 확인</label><input id="confirm" name="confirm" type="password" '
               'autocomplete="new-password" required><p>12자 이상, 영문 대문자·소문자·숫자·기호를 포함한다.</p>') if signup else ''
    return response(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading} · AI 대화 서비스</title><style>{STYLE}</style><main><h1>{heading}</h1><p>{explanation}</p>{notice}
<form method="post" action="/auth/browser/submit"><input type="hidden" name="flow" value="{escape(flow_id)}">
<input type="hidden" name="csrf" value="{escape(csrf)}"><input type="hidden" name="mode" value="{'signup' if signup else 'login'}">
<label for="email">이메일</label><input id="email" name="email" type="email" autocomplete="email" value="{escape(email, quote=True)}" required>
<label for="password">비밀번호</label><input id="password" name="password" type="password" autocomplete="{'new-password' if signup else 'current-password'}" required>{confirm}
<div class="actions"><button class="primary" name="action" value="{'signup' if signup else 'login'}">{heading}</button></div>
<div class="footer"><button class="link" name="action" value="{'switch_login' if signup else 'switch_signup'}" formnovalidate>{'로그인으로 돌아가기' if signup else '계정이 없다면 회원가입'}</button>
<button class="link" name="action" value="cancel" formnovalidate>취소</button></div></form></main></html>''', redirect_uri=redirect_uri)


def logout_card(ticket: str, nonce: str, *, redirect_uri: str) -> HTMLResponse:
    return response(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>로그아웃 · AI 대화 서비스</title><style>{STYLE}</style><main><h1>로그아웃</h1><p>잠시만 기다려 주세요.</p>
<form id="logout" method="post" action="/auth/browser/logout"><input type="hidden" name="ticket" value="{escape(ticket)}">
<div class="actions"><button class="primary">계속</button></div></form></main>
<script nonce="{nonce}">document.getElementById('logout').requestSubmit();</script></html>''', redirect_uri=redirect_uri, nonce=nonce)
