"""HTTP 응답의 줄을 SSE 사건으로 묶는다. 네트워크 조각과 사건 경계는 같지 않다."""
import json


def events(lines):
    name, parts = 'message', []
    for line in lines:
        if not line:
            if parts:
                value = json.loads('\n'.join(parts))
                if not isinstance(value, dict):
                    raise ValueError('JSON object required')
                yield name, value
            name, parts = 'message', []
        elif line.startswith('event:'):
            name = line[6:].strip()
        elif line.startswith('data:'):
            parts.append(line[5:].lstrip(' '))
    if parts:
        raise ValueError('unfinished SSE event')
