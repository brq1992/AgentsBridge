"""M1 echo-only policy, shared by the CLI (handle) and the Agent (final-reply auto-ack)."""
import json

NOTICE_TEXT = 'notice received; non-echo, not executed'


def echo_request_text(body):
    """The text an authorised echo must return, or None when the body is not an M1 echo.

    Only JSON {"op": "echo", "text": T} is an echo; plain text and any other JSON are notices.
    """
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get('op') == 'echo' and isinstance(data.get('text'), str):
        return data['text']
    return None


def echo_text(body):
    """(reply text, outcome) for an executed request under the echo-only policy."""
    text = echo_request_text(body)
    if text is None:
        return NOTICE_TEXT, 'blocked'
    return text, 'completed'
