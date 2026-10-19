"""같은 논리적 전송의 최초 본문·UUID를 확인한 응답까지 보존한다."""
from uuid import uuid4
from clients.api_client import ApiError

class MessageDelivery:
    def __init__(self,api,pending: dict):
        self.api,self.pending = api,pending

    def prepare(self,conversation_id: int,body: dict) -> dict:
        # 최초 body 전체를 복사해 초안 정리 옵션 session_id·session_revision도 보존한다.
        # 같은 본문인지는 role·content만 비교한다. 기존 전송 번호와 옵션은 바꾸지 않는다.
        existing = self.pending.get(conversation_id)
        if existing is not None and any(existing[key]!=body[key] for key in ('role','content')):
            raise ApiError(409,'PENDING_MESSAGE_CHANGED','이전 전송 결과를 확인한다. 같은 번호로 다른 글을 보내지 않는다.')
        if existing is None:
            existing = {**body,'client_request_id':str(uuid4())}
            self.pending[conversation_id] = existing
        return existing

    def confirm(self,conversation_id: int,result: dict) -> dict:
        row = result.get('message') if isinstance(result,dict) else None
        if (not isinstance(row,dict) or type(row.get('id')) is not int
                or row.get('role') not in ('user','assistant') or not isinstance(row.get('content'),str)
                or type(result.get('cache_cleared')) is not bool):
            raise ApiError(502,'RESPONSE_ERROR','전송 응답을 확인하지 못했다. 같은 글과 번호를 유지한다.')
        self.pending.pop(conversation_id,None)
        return result

    def send(self,conversation_id: int,body: dict) -> dict:
        existing = self.prepare(conversation_id,body)
        result = self.api.request('POST',f'/conversations/{conversation_id}/messages',body=existing)
        return self.confirm(conversation_id,result)
