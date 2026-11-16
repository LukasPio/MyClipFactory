import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class PublishError(RuntimeError):
    def __init__(self, message, retry=False, delay=30):
        super().__init__(message)
        self.retry = retry
        self.delay = delay


@dataclass
class Result:
    external_id: str | None = None
    url: str | None = None
    wait: int = 20
    complete: bool = False
    note: str | None = None


def request(client, method, url, allowed=(200,), **kwargs):
    try:
        response = client.request(method, url, **kwargs)
    except httpx.RequestError as error:
        raise PublishError(f"Falha de comunicação com a plataforma ({type(error).__name__}).", retry=True) from error
    if response.status_code not in allowed:
        delay = response.headers.get("Retry-After", "")
        seconds = min(3600, max(15, int(delay))) if delay.isdecimal() else 30
        raise PublishError(f"A plataforma respondeu HTTP {response.status_code}.",
                           retry=response.status_code == 429 or response.status_code >= 500, delay=seconds)
    return response


def data(response):
    try:
        value = response.json()
    except ValueError as error:
        raise PublishError("Resposta inválida da plataforma.", retry=True) from error
    if not isinstance(value, dict):
        raise PublishError("A plataforma devolveu um formato inesperado.")
    return value


def remote_id(value):
    given = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", given):
        raise PublishError("Identificador remoto ausente ou inválido.")
    return given


def require_https(url, hosts):
    target = urlsplit(url)
    host = target.hostname or ""
    if target.scheme != "https" or target.username or target.password or target.port not in (None, 443):
        raise PublishError("A plataforma forneceu uma URL de upload inválida.")
    if not any(host == domain or host.endswith("." + domain) for domain in hosts):
        raise PublishError("A plataforma forneceu um domínio de upload inesperado.")
    return url


def tags(fields):
    return ["#" + re.sub(r"[^\w]", "", tag.lstrip("#")) for tag in fields["hashtags"]
            if re.sub(r"[^\w]", "", tag)]


def caption(fields, field, limit):
    return " ".join(filter(None, [fields[field].strip(), *tags(fields)]))[:limit]


def bearer(token):
    return {"Authorization": "Bearer " + token}
