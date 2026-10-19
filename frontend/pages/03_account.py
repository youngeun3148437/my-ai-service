"""프로필·로그아웃을 대화와 구분한 계정 화면이다."""
from ui.common import current_api
from ui.account_views import account

account(current_api())
