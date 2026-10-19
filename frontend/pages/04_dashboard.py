"""로그인한 계정의 기간별 요청·평가 대시보드를 조립한다."""

from ui.dashboard_views import dashboard
from ui.common import current_api

dashboard(current_api())
