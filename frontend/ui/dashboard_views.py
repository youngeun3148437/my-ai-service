"""기간 집계는 요약하고 요청 기록·통계·평가를 필요한 화면에서 확인한다."""
from datetime import datetime, timedelta
from hashlib import sha256
from time import monotonic
import streamlit as st
from clients.api_client import ApiError
from state.dashboard_state import DashboardController, DashboardState, SEOUL, local_today, query_filters
from ui.realtime_views import realtime_controls

MODES = {"all": "전체", "normal": "전체 답변", "stream": "스트리밍"}
STATUSES = {"all": "전체", "succeeded": "성공", "failed": "실패", "cancelled": "취소"}


def request_error(error: ApiError):
    if error.code == "AUTH_SESSION_ENDED":
        st.session_state.ui_notice = error.message
        st.rerun()


def applied_period(filters: dict) -> str:
    since = datetime.fromisoformat(filters["since"]).astimezone(SEOUL).date()
    until = datetime.fromisoformat(filters["until"]).astimezone(SEOUL).date() - timedelta(days=1)
    return f"{since:%Y-%m-%d} ~ {until:%Y-%m-%d} · {MODES[filters.get('mode', 'all')]} · {STATUSES[filters.get('status', 'all')]}"


def sync_caption(state, *, admin=False, delayed=False):
    stamp = state.last_synced_at
    last = f"마지막 갱신 {stamp:%H:%M:%S}" if stamp is not None else "첫 조회 대기"
    mode = "30초마다 갱신" if admin else "자동 갱신 중"
    if delayed:
        mode = "갱신 지연 · 이전 기록 표시"
    st.caption(f"{mode} · {last}")


def select_status(state, prefix, status):
    # 콜백은 다음 화면 실행보다 먼저 처리되므로 팝오버의 폼 값도 안전하게 함께 바꾼다.
    state.status = status
    st.session_state[f"{prefix}_status"] = status


def filter_form(state, prefix="usage", *, admin=False):
    # 입력 중인 조건과 실제 조회 조건을 구분한다. 성공한 조회 조건은 결과 위에 계속 표시한다.
    submitted = False
    for field in ("start", "end", "mode", "status"):
        st.session_state.setdefault(f"{prefix}_{field}", getattr(state, field))
    with st.container(horizontal=True, vertical_alignment="center"):
        with st.popover("기간·필터", icon=":material/tune:"):
            with st.container(horizontal=True):
                for days in (7, 30):
                    if st.button(f"최근 {days}일", key=f"{prefix}_recent_{days}"):
                        state.end = local_today()
                        state.start = state.end - timedelta(days=days - 1)
                        # 날짜 위젯을 만들기 전에 값을 바꿔 빠른 기간 선택을 폼에도 반영한다.
                        st.session_state[f"{prefix}_start"] = state.start
                        st.session_state[f"{prefix}_end"] = state.end
                        submitted = True
            with st.form(f"{prefix}_filters"):
                left, right = st.columns(2)
                start = left.date_input("시작일", key=f"{prefix}_start")
                end = right.date_input("종료일", key=f"{prefix}_end")
                mode = left.selectbox("생성 방식", list(MODES),
                                      format_func=MODES.get, key=f"{prefix}_mode")
                status = right.selectbox("처리 결과", list(STATUSES),
                                         format_func=STATUSES.get, key=f"{prefix}_status")
                with st.container(horizontal=True, horizontal_alignment="right"):
                    apply = st.form_submit_button("적용", type="primary")
            st.caption("한국 날짜 기준 · 종료일 포함 · 최대 31일")
            if apply:
                state.start, state.end, state.mode, state.status = start, end, mode, status
                submitted = True
        if admin:
            for status, label in (("all", "전체 요청"), ("failed", "실패만")):
                active_status = state.status
                if st.button(label, key=f"{prefix}_quick_{status}",
                             type="primary" if active_status == status else "tertiary",
                             on_click=select_status, args=(state, prefix, status)):
                    submitted = True
    return submitted


def dashboard(api):
    if st.session_state.ui_dashboard_state is None:
        st.session_state.ui_dashboard_state = DashboardState()
    st.header("내 사용 기록")
    st.caption("내 AI 요청의 결과와 사용량을 확인한다.")
    dashboard_panel(api)


@st.fragment(run_every=2, key="release_usage")
def dashboard_panel(api):
    # 필터·알림 확인·결과를 한 영역에서 실행한다. 알림이 와도 메뉴와 페이지 전체는 다시 실행하지 않는다.
    state = st.session_state.ui_dashboard_state
    controller = DashboardController(api, state)
    submitted = filter_form(state)
    if submitted or (state.active_filters is None and not state.error):
        try:
            with st.spinner("사용 기록을 불러오고 있다…"):
                controller.search(query_filters(state.start, state.end, state.mode, state.status))
        except ApiError as error:
            state.error = error.message
            request_error(error)
    if state.aggregate is not None:
        # ready·재연결 뒤의 원본 복구와 30초 대조는 이 fragment 안에서 결과까지 함께 반영한다.
        realtime_controls(api)
    if state.error:
        st.error("사용 기록을 불러오지 못했다. " + state.error)
        st.button("다시 불러오기", key="ui_dashboard_retry", icon=":material/refresh:",
                  on_click=retry_dashboard, args=(controller,))
    if state.aggregate is not None:
        render_results(state, controller)


def retry_dashboard(controller):
    # 재시도 결과를 다음 fragment 실행 전에 준비한다. 오류 표시를 지우기 위한 전체 rerun은 없다.
    state = controller.state
    try:
        if state.active_filters is None:
            controller.search(query_filters(state.start, state.end, state.mode, state.status))
        else:
            controller.recover()
        realtime = st.session_state.ui_realtime_state
        if realtime is not None:
            realtime.synchronized(monotonic())
    except ApiError as error:
        state.error = error.message
        request_error(error)


def render_summary(aggregate, *, admin=False):
    summary = aggregate["summary"]
    total, known = summary["total_requests"], summary["usage_known_requests"]
    columns = st.columns(4 if admin else 3)
    columns[0].metric("전체 요청" if admin else "내 요청", f"{total:,}건")
    offset = 0
    if admin:
        columns[1].metric("실패율", f"{summary['failed'] / total * 100:.1f}%" if total else "—",
                          help="실패 요청 수 ÷ 조회 조건의 전체 요청 수. 취소는 실패에 포함하지 않는다.")
        offset = 1
    average = summary["average_duration_ms"]
    columns[1 + offset].metric("평균 응답 시간", f"{average / 1000:.2f}초" if average is not None else "—")
    columns[2 + offset].metric("사용 토큰", f"{summary['known_total_tokens']:,}" if known else ("미확인" if total else "—"))
    st.caption(f"기간 전체 집계 · 성공 {summary['succeeded']:,}건 · 실패 {summary['failed']:,}건 · 취소 {summary['cancelled']:,}건")
    if total and known < total:
        st.caption(f"토큰 확인 {known:,}/{total:,}건 · 미확인 {total - known:,}건은 0으로 계산하지 않는다.")


def render_daily(aggregate):
    st.subheader("일별 요청")
    daily = aggregate["daily"]
    if not daily:
        st.info("조회 기간에 표시할 요청이 없다.")
        return
    rows = [{"일자": row["date"], "처리 결과": label, "요청 수": row[status]}
            for row in daily for status, label in (("succeeded", "성공"), ("failed", "실패"), ("cancelled", "취소"))]
    st.vega_lite_chart(rows, {
        "mark": {"type": "bar", "cornerRadiusTopLeft": 3, "cornerRadiusTopRight": 3},
        "encoding": {
            "x": {"field": "일자", "type": "ordinal", "axis": {"labelAngle": -45}},
            "y": {"field": "요청 수", "type": "quantitative", "stack": "zero",
                  "scale": {"zero": True, "domainMin": 0, "nice": False},
                  "axis": {"format": "d", "tickMinStep": 1}},
            "color": {"field": "처리 결과", "type": "nominal",
                      "scale": {"domain": ["성공", "실패", "취소"], "range": ["#4476dc", "#d96161", "#a4adbd"]},
                      "legend": {"orient": "bottom"}},
            "tooltip": [{"field": "일자"}, {"field": "처리 결과"}, {"field": "요청 수", "type": "quantitative", "format": "d"}],
        },
    }, width="stretch", height=300)
    st.caption("한국 날짜 기준 · 현재 조회 조건의 기간 전체 요청을 집계한다.")


def render_feedback(aggregate):
    st.subheader("답변 평가")
    feedback = aggregate["feedback"]
    st.caption("평가 작성 기간만 적용한다. 생성 방식·처리 결과 필터는 적용하지 않는다.")
    average = feedback["average_rating"]
    left, right = st.columns(2)
    left.metric("평균 점수", f"{average:.2f} / 5" if average is not None else "평가 없음")
    right.metric("평가 수", f"{feedback['count']:,}건")
    if not feedback["count"]:
        st.info("조회 기간에 등록된 평가가 없다.")
        return
    rows = [{"점수": str(row["rating"]), "평가 수": row["count"]} for row in feedback["ratings"]]
    st.vega_lite_chart(rows, {
        "mark": {"type": "bar", "color": "#4476dc", "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4},
        "encoding": {
            "x": {"field": "점수", "type": "ordinal", "sort": ["1", "2", "3", "4", "5"], "axis": {"labelAngle": 0}},
            "y": {"field": "평가 수", "type": "quantitative",
                  "scale": {"zero": True, "domainMin": 0, "nice": False},
                  "axis": {"format": "d", "tickMinStep": 1}},
            "tooltip": [{"field": "점수"}, {"field": "평가 수", "type": "quantitative", "format": "d"}],
        },
    }, width="stretch", height=230)


def record_display(row, *, admin=False):
    stamp = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).astimezone(SEOUL)
    result = {"시각": stamp.strftime("%m-%d %H:%M"), "결과": STATUSES[row["status"]],
              "방식": MODES[row["mode"]], "응답 시간": f"{row['duration_ms'] / 1000:.2f}초" if row["duration_ms"] is not None else "—",
              "토큰": f"{row['total_tokens']:,}" if row["total_tokens"] is not None else "미확인"}
    return {"요청 번호": row["id"], **result} if admin else result


@st.dialog("요청 상세", width="medium")
def request_details(row, *, admin=False):
    st.caption("요청 메타데이터 · 질문·답변·파일 본문은 포함하지 않는다.")
    overview, identifiers = st.tabs(["처리 정보", "식별 정보"])
    with overview:
        brief = record_display(row)
        brief.update({"모델": row.get("model"), "HTTP 상태": row.get("http_status"),
                      "오류 코드": row.get("error_code") or "—", "입력 토큰": row.get("input_tokens"),
                      "출력 토큰": row.get("output_tokens"), "문맥 메시지 수": row.get("context_message_count"),
                      "첨부 수": row.get("attachment_count")})
        st.table([{"항목": key, "값": str(value) if value is not None else "미확인"}
                  for key, value in brief.items()])
    with identifiers:
        fields = (("요청 번호", "id"), ("AI 시도 ID", "attempt_id"), ("HTTP 요청 ID", "http_request_id"),
                  ("대화 번호", "conversation_id"), ("질문 번호", "question_message_id"),
                  ("답변 번호", "answer_message_id"))
        if admin:
            fields = (("사용자 Auth UUID", "auth_user_id"),) + fields
        st.table([{"항목": title, "값": str(row[key]) if row.get(key) is not None else "미확인"}
                  for title, key in fields])
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("닫기", key="ui_request_detail_close"):
            st.rerun()


def render_records(state, controller, *, admin=False):
    prefix = "operations" if admin else "usage"
    st.subheader("최근 요청")
    st.caption(f"불러온 기록 {len(state.records):,}건 · 상단 요약은 페이지 수와 관계없이 기간 전체 집계다.")
    if state.records:
        # 데이터가 교체되면 선택을 초기화한다. 같은 표의 단순 재실행에서는 상세 창을 반복해서 열지 않는다.
        signature = sha256(repr((state.active_filters, [row["id"] for row in state.records])).encode()).hexdigest()[:12]
        event = st.dataframe([record_display(row, admin=admin) for row in state.records],
                             width="stretch", hide_index=True, key=f"{prefix}_records_{signature}",
                             on_select="rerun", selection_mode="single-row", row_height=44,
                             column_config={"시각": st.column_config.TextColumn(width="medium"),
                                            "결과": st.column_config.TextColumn(width="small"),
                                            "방식": st.column_config.TextColumn(width="small"),
                                            "응답 시간": st.column_config.TextColumn(width="small"),
                                            "토큰": st.column_config.TextColumn(width="small")})
        selected = event.selection.rows
        selection_key = f"{prefix}_detail_selection"
        identity = (signature, selected[0]) if selected else None
        previous = st.session_state.get(selection_key)
        st.session_state[selection_key] = identity
        if selected:
            row = state.records[selected[0]]
            with st.container(horizontal=True, horizontal_alignment="right"):
                reopen = st.button("선택한 요청 상세", icon=":material/open_in_new:", key=f"{prefix}_detail_open", type="tertiary")
            if identity != previous or reopen:
                request_details(row, admin=admin)
        else:
            st.caption("행을 선택하면 요청 상세를 확인할 수 있다.")
    else:
        st.info("이 조건에 해당하는 요청 기록이 없다.")
    if state.next_before_id is not None:
        def load_more():
            # 위젯 콜백은 결과를 그리기 전에 실행한다. 불러온 목록을 보여 주기 위한 전체 rerun이 필요 없다.
            try:
                controller.more()
            except ApiError as error:
                request_error(error)
        with st.container(horizontal=True, horizontal_alignment="center"):
            st.button("이전 요청 더 보기", icon=":material/expand_more:", key="ui_dashboard_more", on_click=load_more)
    elif state.records:
        st.caption("조회 기간의 요청을 모두 불러왔다.")


def render_results(state, controller, *, admin=False):
    st.caption(f"현재 조회: {applied_period(state.active_filters)}")
    if state.error:
        st.caption("표시한 조건의 이전 성공 결과를 유지한다.")
    render_summary(state.aggregate, admin=admin)
    records_tab, daily_tab, rating_tab = st.tabs(["요청 기록", "통계", "답변 평가"])
    with records_tab:
        render_records(state, controller, admin=admin)
    with daily_tab:
        render_daily(state.aggregate)
    with rating_tab:
        render_feedback(state.aggregate)
