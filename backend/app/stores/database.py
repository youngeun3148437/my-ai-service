"""요청 사용자의 Supabase 클라이언트로 chat_lab 테이블의 CRUD를 실행한다."""

from app.core.dependencies import UserContext
from app.core.errors import ServiceError, first, query_data, query_rows

PROFILE_COLUMNS = "id,auth_user_id,display_name"
CONVERSATION_COLUMNS = "id,user_id,title,created_at"
MESSAGE_COLUMNS = "id,conversation_id,role,content,created_at"
ATTACHMENT_COLUMNS = ('id,conversation_id,auth_user_id,filename,content_type,byte_size,'
                      'status,object_path,sha256,created_at')
FEEDBACK_COLUMNS = 'id,answer_message_id,rating,comment,client_request_id,created_at'
LOG_COLUMNS = ('id,attempt_id,http_request_id,auth_user_id,conversation_id,question_message_id,answer_message_id,'
               'model,status,mode,http_status,started_at,duration_ms,input_tokens,output_tokens,total_tokens,'
               'context_message_count,attachment_count,error_code,created_at')


class Database:
    def __init__(self, context: UserContext, attachments_enabled: bool = False,
                 cache_version_enabled: bool = False):
        self.context = context
        self.client = context.client
        self.attachments_enabled = attachments_enabled
        self.cache_version_enabled = cache_version_enabled
        # schema는 클라이언트 생성 시 지정한다. 요청 중 별도 HTTP 풀을 만들지 않는다.

    async def profile(self) -> dict:
        return first(await query_rows(self.client.table("users").select(PROFILE_COLUMNS)
                    .eq("auth_user_id", str(self.context.auth_user_id)).limit(1)), "프로필")

    async def create_profile(self, name: str) -> dict:
        return first(await query_rows(self.client.table("users").insert({
            "display_name": name, "auth_user_id": str(self.context.auth_user_id),
        }).select(PROFILE_COLUMNS)), "생성된 프로필")

    async def update_profile(self, name: str) -> dict:
        return first(await query_rows(self.client.table("users").update({"display_name": name})
                    .eq("auth_user_id", str(self.context.auth_user_id)).select(PROFILE_COLUMNS)), "프로필")

    async def conversation(self, conversation_id: int) -> dict:
        # Redis 조회 전에도 RLS를 통과한 현재 대화인지 확인한다.
        # 버전은 이 소유권 조회에서 함께 읽는다. 설정이 꺼지면 기존 열만 요구한다.
        columns = CONVERSATION_COLUMNS + (",cache_version" if self.cache_version_enabled else "")
        return first(await query_rows(self.client.table("conversations").select(columns)
                    .eq("id", conversation_id).limit(1)), "대화")

    async def conversations(self, before_id: int | None, limit: int) -> list[dict]:
        query = self.client.table("conversations").select(CONVERSATION_COLUMNS).order("id", desc=True)
        if before_id is not None:
            query = query.lt("id", before_id)
        return await query_rows(query.limit(limit))

    async def create_conversation(self, title: str) -> dict:
        profile = await self.profile()
        return first(await query_rows(self.client.table("conversations").insert({
            "user_id": profile["id"], "title": title,
        }).select(CONVERSATION_COLUMNS)), "생성된 대화")

    async def update_conversation(self, conversation_id: int, title: str) -> dict:
        return first(await query_rows(self.client.table("conversations").update({"title": title})
                    .eq("id", conversation_id).select(CONVERSATION_COLUMNS)), "대화")

    async def delete_conversation(self, conversation_id: int) -> None:
        # 파일은 Storage API로 지워야 한다. FK RESTRICT도 동시 첨부 생성과 삭제의 경합을 막는다.
        if self.attachments_enabled and await self.attachments(conversation_id):
            raise ServiceError(409, 'ATTACHMENTS_REMAIN', '첨부 파일을 먼저 삭제한 뒤 대화를 삭제한다')
        first(await query_rows(self.client.table("conversations").delete()
              .eq("id", conversation_id).select("id")), "대화")

    async def messages(self, conversation_id: int, before_id: int | None, limit: int) -> list[dict]:
        query = self.client.table("messages").select(MESSAGE_COLUMNS).eq("conversation_id", conversation_id)
        if before_id is not None:
            query = query.lt("id", before_id)
        # limit+1개를 읽어 다음 페이지가 있는지 판단한다. API는 시간 대신 ID로 이어 읽는다.
        return await query_rows(query.order("id", desc=True).limit(limit + 1))

    async def create_message(self, conversation_id: int, values: dict) -> dict:
        # INSERT의 RLS가 접근을 검사한다. select는 별도 조회가 아닌 저장 결과 열이다.
        return first(await query_rows(self.client.table("messages").insert({
            "conversation_id": conversation_id, **values,
        }).select(MESSAGE_COLUMNS)), "생성된 메시지")

    async def create_message_once(self, conversation_id: int, values: dict, request_id) -> tuple[dict, bool]:
        # 입력은 대화 ID·최초 role/content·전송 UUID다. 초안 ID·버전은 RPC 본문에 넣지 않는다.
        # RPC(Remote Procedure Call)는 DB 함수를 HTTP로 호출해 접수 기록과 INSERT를 한 트랜잭션에 묶는다.
        # 같은 UUID·본문이면 기존 message와 replayed=True, 본문이 다르면 409를 받는다.
        try:
            # 접수 RPC에 원본 본문과 UUID를 보내 저장 결과·재사용 여부를 받는다.
            result = await query_data(self.client.rpc("create_message_once", {
                "p_conversation_id": conversation_id, "p_client_request_id": str(request_id),
                "p_role": values["role"], "p_content": values["content"],
            }))
        except ServiceError as error:
            if error.status_code == 410 and error.detail['code'] == 'REQUEST_RESULT_REMOVED':
                self.removed_request(error)
            raise
        # RPC 성공 본문도 검증한다. 저장 원본과 재사용 여부를 서비스에 함께 돌려준다.
        if not isinstance(result, dict) or not isinstance(result.get("message"), dict) or not isinstance(result.get("replayed"), bool):
            raise ServiceError(502, "DATA_UNAVAILABLE", "전송 접수 결과를 확인할 수 없다")
        return result["message"], result["replayed"]

    async def answer_context(self, conversation_id: int, question_id: int, limit: int) -> list[dict]:
        # 같은 대화에서 지정 질문까지 읽는다. RLS가 다른 사용자의 원본을 제외한다.
        # 내림차순 첫 행이 question_id인지 서비스에서 확인하고 시간 순서로 뒤집는다.
        return await query_rows(self.client.table("messages").select(MESSAGE_COLUMNS)
                                .eq("conversation_id", conversation_id).lte("id", question_id)
                                .order("id", desc=True).limit(limit + 1))

    async def update_message(self, message_id: int, content: str) -> dict:
        # 결과 행의 conversation_id를 서비스가 받아 해당 대화의 캐시를 지운다.
        return first(await query_rows(self.client.table("messages").update({"content": content})
                    .eq("id", message_id).select(MESSAGE_COLUMNS)), "메시지")

    async def delete_message(self, message_id: int) -> dict:
        return first(await query_rows(self.client.table("messages").delete()
                    .eq("id", message_id).select(MESSAGE_COLUMNS)), "메시지")

    async def attachments(self, conversation_id: int) -> list[dict]:
        return await query_rows(self.client.table('attachments').select(ATTACHMENT_COLUMNS)
                                .eq('conversation_id', conversation_id).order('created_at'))

    async def attachment(self, conversation_id: int, attachment_id) -> dict | None:
        rows = await query_rows(self.client.table('attachments').select(ATTACHMENT_COLUMNS)
                                .eq('conversation_id', conversation_id).eq('id', str(attachment_id)).limit(1))
        return rows[0] if rows else None

    async def create_attachment(self, values: dict) -> dict:
        return first(await query_rows(self.client.table('attachments').insert(values)
                                      .select(ATTACHMENT_COLUMNS)), '생성된 첨부')

    async def ready_attachment(self, conversation_id: int, attachment_id) -> dict:
        return first(await query_rows(self.client.table('attachments').update({'status': 'ready'})
            .eq('conversation_id', conversation_id).eq('id', str(attachment_id))
            .select(ATTACHMENT_COLUMNS)), '첨부')

    async def delete_attachment(self, conversation_id: int, attachment_id) -> None:
        first(await query_rows(self.client.table('attachments').delete()
            .eq('conversation_id', conversation_id).eq('id', str(attachment_id)).select('id')), '첨부')

    async def answer(self, answer_id: int) -> dict:
        return first(await query_rows(self.client.table('messages').select(MESSAGE_COLUMNS)
                                      .eq('id', answer_id).limit(1)), '답변')

    async def feedbacks(self, answer_id: int, before_id: int | None, limit: int) -> list[dict]:
        query = self.client.table('answer_feedback').select(FEEDBACK_COLUMNS).eq('answer_message_id', answer_id)
        if before_id is not None:
            query = query.lt('id', before_id)
        return await query_rows(query.order('id', desc=True).limit(limit + 1))

    async def create_feedback(self, answer_id: int, rating: int, comment: str) -> dict:
        return first(await query_rows(self.client.table('answer_feedback').insert({
            'answer_message_id': answer_id, 'rating': rating, 'comment': comment,
        }).select(FEEDBACK_COLUMNS)), '피드백')

    async def create_feedback_once(self, answer_id: int, body) -> tuple[dict, bool]:
        try:
            result = await query_data(self.client.rpc('create_feedback_once', {
                'p_answer_message_id': answer_id, 'p_rating': body.rating, 'p_comment': body.comment,
                'p_client_request_id': str(body.client_request_id),
            }))
        except ServiceError as error:
            if error.detail['code'] == 'REQUEST_ID_REUSED':
                raise ServiceError(409, 'FEEDBACK_REQUEST_REUSED', '같은 평가 번호에는 처음 보낸 점수와 의견을 사용한다') from error
            raise
        if not isinstance(result, dict) or not isinstance(result.get('feedback'), dict) or not isinstance(result.get('replayed'), bool):
            raise ServiceError(502, 'DATA_UNAVAILABLE', '평가 접수 결과를 확인할 수 없다')
        return result['feedback'], result['replayed']

    async def create_ai_log(self, values: dict) -> int:
        # 원문 본문 없이 종료 상태만 INSERT한다. 답변 INSERT와는 별도 작업이다.
        row = first(await query_rows(self.client.table('ai_request_logs').insert(values).select('id')), 'AI 요청 로그')
        return row['id']

    async def ai_logs(self, filters, before_id: int | None, limit: int) -> list[dict]:
        query = self.client.table('ai_request_logs').select(LOG_COLUMNS)
        for name in ('status', 'mode'):
            if value := getattr(filters, name):
                query = query.eq(name, value)
        if filters.since is not None:
            query = query.gte('started_at', filters.since.isoformat())
        if filters.until is not None:
            query = query.lt('started_at', filters.until.isoformat())
        if before_id is not None:
            query = query.lt('id', before_id)
        return await query_rows(query.order('id', desc=True).limit(limit + 1))

    async def dashboard(self, filters) -> dict:
        # RPC에 LIMIT를 전달하지 않는다. 페이지 밖 기록도 전체 기간 집계에 포함된다.
        return await query_data(self.client.rpc('ai_dashboard', {
            'p_since': filters.since.isoformat() if filters.since is not None else None,
            'p_until': filters.until.isoformat() if filters.until is not None else None,
            'p_status': filters.status, 'p_mode': filters.mode,
        }))

    @staticmethod
    def removed_request(error: ServiceError) -> None:
        # 삭제된 원본의 접수 오류를 410으로 전달한다. 같은 전송 번호로 다시 INSERT하지 않는다.
        raise ServiceError(410, "REQUEST_RESULT_REMOVED", "이 전송의 원본은 삭제되었다. 새 메시지로 자동 복원하지 않는다") from error
