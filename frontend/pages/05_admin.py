"""관리자 전용 서비스 지표 화면이다. 실제 권한은 FastAPI가 매번 검사한다."""
from ui.admin_views import admin_dashboard
from ui.common import current_api

admin_dashboard(current_api())
