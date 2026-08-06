"""Remote (Agent-to-Agent) sub-agents.

A sub-agent can delegate to an external HTTP agent (LangGraph, CrewAI, a custom service)
instead of running a local model completion. The remote configuration lives in the
sub-agent's `meta.remote` block (see `resolve_remote_config`).

Two protocols are supported:

* ``generic`` - POST a JSON body to the configured URL and read the result back. The body
  carries the task under several common key names so most agent servers work unmodified.
* ``a2a`` - the same invocation, but the agent card at ``/.well-known/agent.json`` is used
  for discovery (name/description) when verifying the connection.

Security: the URL is user-supplied, so requests made on behalf of a **non-admin** owner are
routed through the SSRF-safe session and pre-validated with ``validate_url``. Admin-owned
agents may reach internal hosts, mirroring the tool-server precedent (see
``backend/open_webui/routers/tools.py`` for the rationale).
"""

import json
import logging
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.retrieval.web.utils import get_ssrf_safe_session, validate_url

log = logging.getLogger(__name__)

DEFAULT_REMOTE_TIMEOUT = 300
MAX_REMOTE_TIMEOUT = 1800

# Response keys checked in order when unwrapping a remote agent's JSON reply.
_RESULT_KEYS = ('output', 'result', 'content', 'summary', 'answer', 'response', 'text')


def resolve_remote_config(subagent) -> Optional[dict]:
    """Return the sub-agent's remote block when remote delegation is enabled, else None."""
    if not subagent:
        return None
    try:
        meta = subagent.meta.model_dump() if subagent.meta else {}
    except AttributeError:
        meta = subagent.meta if isinstance(subagent.meta, dict) else {}

    remote = (meta or {}).get('remote') or {}
    if not isinstance(remote, dict) or not remote.get('enabled'):
        return None
    if not (remote.get('url') or '').strip():
        return None
    return remote


def enforce_ssrf(owner_role: Optional[str]) -> bool:
    """Whether to apply SSRF protection for a remote agent owned by ``owner_role``.

    Admin-owned agents may reach internal/localhost services (matching tool servers);
    everyone else is validated and routed through the rebinding-safe resolver.
    """
    return owner_role != 'admin'


def resolve_timeout(remote: dict) -> int:
    try:
        timeout = int(remote.get('timeout') or DEFAULT_REMOTE_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_REMOTE_TIMEOUT
    return max(1, min(timeout, MAX_REMOTE_TIMEOUT))


def agent_card_url(url: str) -> str:
    """The well-known agent-card URL for the origin of ``url``."""
    parsed = urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    return urljoin(origin, '/.well-known/agent.json')


def _session(ssrf_safe: bool, timeout: int) -> aiohttp.ClientSession:
    if ssrf_safe:
        # Re-validates the connect-time IP, defeating DNS rebinding.
        return get_ssrf_safe_session()
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
        trust_env=True,
    )


def extract_summary(payload: Any) -> str:
    """Unwrap a remote agent's reply into plain text.

    Handles the common shapes: a bare string, ``{"output": ...}`` (langserve),
    ``{"messages": [...]}`` (LangGraph state), and OpenAI-style ``choices``.
    """
    if payload is None:
        return ''
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        # A bare message list (LangGraph can return the state's message array).
        return extract_summary({'messages': payload})
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    for key in _RESULT_KEYS:
        if key in payload:
            value = payload[key]
            if isinstance(value, (dict, list)):
                # e.g. {"output": {"messages": [...]}} - recurse into the wrapper.
                nested = extract_summary(value)
                if nested:
                    return nested
                continue
            if value is not None and str(value).strip():
                return str(value)

    messages = payload.get('messages')
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, str):
            return last
        if isinstance(last, dict):
            content = last.get('content')
            if isinstance(content, list):
                # Content-parts array (Anthropic/OpenAI style).
                return ''.join(
                    str(part.get('text', ''))
                    for part in content
                    if isinstance(part, dict) and part.get('type') in ('text', 'output_text')
                )
            if content is not None:
                return str(content)

    choices = payload.get('choices')
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get('message') or {}
            if isinstance(message, dict) and message.get('content') is not None:
                return str(message['content'])
            if first.get('text') is not None:
                return str(first['text'])

    # Nothing recognised - hand back compact JSON so the lead agent still sees the payload.
    return json.dumps(payload, ensure_ascii=False)


async def _read_body(response: aiohttp.ClientResponse) -> Any:
    text = await response.text()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


async def fetch_agent_card(
    url: str, headers: Optional[dict] = None, *, ssrf_safe: bool = True, timeout: int = 10
) -> Optional[dict]:
    """Best-effort fetch of the A2A agent card. Returns None when unavailable."""
    card_url = agent_card_url(url)
    if ssrf_safe:
        validate_url(card_url)

    try:
        async with _session(ssrf_safe, timeout) as session:
            async with session.get(
                card_url,
                headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                if response.status != 200:
                    return None
                body = await _read_body(response)
                return body if isinstance(body, dict) else None
    except Exception as e:
        log.debug(f'Agent card unavailable at {card_url}: {e}')
        return None


def build_payload(task: str, context: str, metadata: Optional[dict] = None) -> dict:
    """The generic invocation body.

    The task is sent under several key names (``task``/``input``/``message``/``messages``)
    so that most agent servers can consume it without a custom adapter.
    """
    prompt = f'{task}\n\n## Context\n{context}' if context else task
    return {
        'task': task,
        'context': context or '',
        'input': prompt,
        'message': prompt,
        'messages': [{'role': 'user', 'content': prompt}],
        'metadata': metadata or {},
    }


async def invoke_remote_agent(
    *,
    remote: dict,
    task: str,
    context: str = '',
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    metadata: Optional[dict] = None,
    ssrf_safe: bool = True,
    max_output: Optional[int] = None,
) -> dict:
    """Call the remote agent and return ``{'status', 'summary', 'error'}``.

    Mirrors the local sub-agent result shape so the delegation plumbing is unchanged.
    """
    url = (remote.get('url') or '').strip()
    if not url:
        return {'status': 'error', 'summary': '', 'error': 'Remote agent URL is not configured.'}

    timeout = resolve_timeout(remote)

    if ssrf_safe:
        try:
            validate_url(url)
        except Exception as e:
            log.warning(f'Blocked remote agent URL {url}: {e}')
            return {
                'status': 'error',
                'summary': '',
                'error': (
                    'Remote agent URL is not allowed. Public URLs only; ask an admin to enable '
                    'local network access to reach an internal host.'
                ),
            }

    payload = build_payload(task, context, metadata)

    try:
        async with _session(ssrf_safe, timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json', **(headers or {})},
                cookies=cookies or {},
                timeout=aiohttp.ClientTimeout(total=timeout),
                # Redirects are refused so a 302 cannot bounce a validated URL to an internal host.
                allow_redirects=False,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                body = await _read_body(response)

                if response.status >= 400:
                    detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                    return {
                        'status': 'error',
                        'summary': '',
                        'error': f'Remote agent returned HTTP {response.status}: {str(detail)[:500]}',
                    }
                if response.status in (301, 302, 303, 307, 308):
                    return {
                        'status': 'error',
                        'summary': '',
                        'error': 'Remote agent attempted a redirect, which is not allowed.',
                    }

                summary = (extract_summary(body) or '').strip()
                if max_output and len(summary) > max_output:
                    summary = f'{summary[:max_output]}\n\n[output truncated]'

                return {
                    'status': 'completed',
                    'summary': summary or 'Remote agent produced no output.',
                    'error': None,
                }
    except aiohttp.ClientConnectorError as e:
        return {'status': 'error', 'summary': '', 'error': f'Could not reach the remote agent: {e}'}
    except TimeoutError:
        return {
            'status': 'error',
            'summary': '',
            'error': f'Remote agent timed out after {timeout}s.',
        }
    except Exception as e:
        log.exception(f'Remote agent call failed: {e}')
        return {'status': 'error', 'summary': '', 'error': f'Remote agent call failed: {e}'}
