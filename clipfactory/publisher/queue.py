import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken

from ..store import dump, uid, unpack
from . import instagram, oauth, tiktok, youtube
from .base import PublishError

ADAPTERS = {"youtube": youtube, "tiktok": tiktok, "instagram": instagram}


def media_signature(settings, publication, expires):
    message = f"{publication}:{expires}".encode()
    return hmac.new(settings.key.encode(), message, hashlib.sha256).hexdigest()


def signed_media(settings, publication):
    root = urlsplit(settings.public_origin)
    if root.scheme != "https" or not root.hostname or root.path or root.username or root.query or root.fragment:
        raise PublishError("Configure MCF_MEDIA_HTTPS_ORIGIN com uma origem HTTPS pública.")
    if not settings.key:
        raise PublishError("Configure MCF_TOKEN_KEY para assinar a URL de mídia.")
    expires = int(time.time()) + 3600
    signature = media_signature(settings, publication, expires)
    return f"{settings.public_origin}/media/{publication}?" + urlencode({"e": expires, "s": signature})


def begin(store):
    now = time.time()
    with store.tx(True) as conn:
        row = conn.execute(
            """SELECT p.* FROM publications p JOIN accounts a ON a.id=p.account_id
               WHERE p.approved IS NOT NULL AND p.due<=? AND p.retry_at<=?
               AND (p.state IN ('APPROVED','SCHEDULED') OR (p.state='PROCESSING' AND p.lease<=?))
               AND NOT EXISTS (SELECT 1 FROM publications q WHERE q.id<>p.id AND q.account_id=p.account_id
                               AND q.state='PROCESSING' AND q.lease>?)
               ORDER BY p.created LIMIT 1""", (now, now, now, now),
        ).fetchone()
        if row is None:
            return None
        owner = uid()
        conn.execute("UPDATE publications SET state='PROCESSING',owner=?,lease=? WHERE id=?",
                     (owner, now + 900, row["id"]))
        return unpack(row, "fields") | {"owner": owner}


def work(store, settings, client=None):
    job = begin(store)
    if not job:
        return False
    checkpoint = {}
    identifier = job["id"]
    owner = job["owner"]
    with store.tx() as conn:
        account = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (job["account_id"],)).fetchone())
    platform = account["platform"]

    def save(**updates):
        checkpoint.update(updates)
        with store.tx(True) as conn:
            encrypted = oauth.vault(settings).encrypt(dump(checkpoint).encode()).decode()
            result = conn.execute("UPDATE publications SET checkpoint=?,lease=? WHERE id=? AND owner=?",
                                  (encrypted, time.time() + 900, identifier, owner))
            if not result.rowcount:
                raise PublishError("A reserva da publicação expirou.")

    def execute(http):
        adapter = ADAPTERS[platform]
        path = settings.file("clips", f"{job['clip_id']}-{job['revision']}.mp4")
        if not path.is_file():
            raise PublishError("O MP4 aprovado não está disponível no disco.")
        token = oauth.access(account, http, settings, store)
        return adapter.publish(http, token, account, path, job["fields"], checkpoint, save,
                               settings, lambda: signed_media(settings, identifier))

    try:
        if job["checkpoint"] != "{}":
            try:
                checkpoint = json.loads(oauth.vault(settings).decrypt(job["checkpoint"].encode()))
            except InvalidToken as error:
                raise PublishError("A sessão de envio não pode ser recuperada com a chave atual.") from error
        if time.time() > max(job["created"], job["due"]) + 86400:
            raise PublishError("A plataforma não confirmou a publicação em até 24 horas.")
        if client is None:
            with httpx.Client(timeout=httpx.Timeout(60, connect=12)) as http:
                result = execute(http)
        else:
            result = execute(client)
        with store.tx(True) as conn:
            if result.external_id or result.complete:
                conn.execute("""UPDATE publications SET state='PUBLISHED',external_id=?,url=?,error=?,
                              lease=NULL,owner=NULL WHERE id=? AND owner=?""",
                             (result.external_id, result.url, result.note, identifier, owner))
            else:
                conn.execute("""UPDATE publications SET retry_at=?,lease=0,owner=NULL,error=NULL
                              WHERE id=? AND owner=?""", (time.time() + result.wait, identifier, owner))
    except (PublishError, KeyError, ValueError, OSError) as error:
        attempts = job["attempts"] + 1
        retry = isinstance(error, PublishError) and error.retry and attempts < 7
        detail = str(error) if isinstance(error, PublishError) else "Resposta ou arquivo incompatível com o adaptador."
        delay = max(error.delay, 2**attempts * 10) if retry else 0
        with store.tx(True) as conn:
            conn.execute("""UPDATE publications SET state=?,attempts=?,error=?,retry_at=?,lease=0,owner=NULL
                          WHERE id=? AND owner=?""",
                         ("PROCESSING" if retry else "FAILED", attempts, detail,
                          time.time() + delay, identifier, owner))
    return True
