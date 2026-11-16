import base64
import binascii
import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .media import MediaError, probe
from .publisher import oauth, queue, tiktok
from .publisher.base import PublishError
from .schemas import Edit, Metrics, Processing, Publish, Review
from .store import Store, dump, uid, unpack

logger = logging.getLogger(__name__)


def one(conn, table, identifier):
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, "Registro não encontrado.")
    return row


def revoke(conn, clip):
    if conn.execute("SELECT 1 FROM publications WHERE clip_id=? AND state='PROCESSING'", (clip,)).fetchone():
        raise HTTPException(409, "Aguarde a conclusão da publicação em andamento.")
    conn.execute("""UPDATE publications SET state='FAILED',approved=NULL,error='Aprovação revogada na revisão.'
                  WHERE clip_id=? AND state IN ('APPROVED','SCHEDULED','PENDING')""", (clip,))


def current_job(conn, clip):
    return conn.execute("""SELECT id FROM jobs WHERE clip_id=? AND state IN ('QUEUED','RUNNING')""",
                        (clip,)).fetchone()


def matches(password, header):
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        user, secret = base64.b64decode(encoded, validate=True).split(b":", 1)
        return user == b"operator" and hmac.compare_digest(secret, password.encode())
    except (ValueError, binascii.Error):
        return False


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings)
    static = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifetime(app):
        store.init()
        yield

    app = FastAPI(title="MyClipFactory", lifespan=lifetime, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/media/"):
            return await call_next(request)
        expected = urlsplit(settings.origin).netloc
        if request.headers.get("host", "").lower() != expected:
            return JSONResponse({"detail": "Origem de acesso diferente de MCF_ORIGIN."}, status_code=403)
        if not settings.password and request.client and request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"detail": "O modo sem senha aceita somente acesso local."}, status_code=403)
        if settings.password and not matches(settings.password, request.headers.get("authorization", "")):
            return JSONResponse({"detail": "Autenticação obrigatória."}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="MyClipFactory"'})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (origin and origin != settings.origin) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Origem da alteração não autorizada."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PublishError)
    async def publish_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/")
    def dashboard():
        return FileResponse(static / "index.html")

    @app.post("/api/videos", status_code=201)
    async def upload(request: Request, name: str = Query(min_length=1, max_length=180)):
        if not name.lower().endswith(".mp4") or request.headers.get("content-type", "").split(";")[0] != "video/mp4":
            raise HTTPException(415, "Envie um arquivo .mp4 como video/mp4.")
        size = 0
        length = request.headers.get("content-length", "")
        if length.isdecimal() and int(length) > settings.max_bytes:
            raise HTTPException(413, "O arquivo excede o limite desta instalação.")
        identifier = uid()
        temp = settings.file("sources", identifier + ".part")
        target = temp.with_suffix(".mp4")
        try:
            with temp.open("xb") as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_bytes:
                        raise HTTPException(413, "O arquivo excede o limite desta instalação.")
                    await run_in_threadpool(file.write, chunk)
            try:
                info = await run_in_threadpool(probe, temp, settings.max_hours * 3600)
            except MediaError as error:
                raise HTTPException(422, str(error)) from error
            temp.replace(target)
            with store.tx(True) as conn:
                conn.execute("INSERT INTO videos VALUES(?,?,?,?,?)", (identifier, Path(name).name, size, dump(info), time.time()))
        except BaseException:
            temp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return {"id": identifier, "name": Path(name).name, "info": info}

    @app.get("/api/library")
    def library():
        with store.tx() as conn:
            videos = [unpack(row, "info") for row in conn.execute("SELECT * FROM videos ORDER BY created DESC")]
            clips = [unpack(row, "candidate", "fields") for row in conn.execute(
                "SELECT * FROM clips ORDER BY created DESC")]
            jobs = [unpack(row, "config") for row in conn.execute(
                """SELECT id,video_id,clip_id,kind,config,state,progress,message,created,cancel
                   FROM jobs ORDER BY created DESC LIMIT 200""")]
            accounts = [dict(row) for row in conn.execute("SELECT id,platform,label FROM accounts ORDER BY label")]
            publications = [dict(row) for row in conn.execute(
                """SELECT p.id,p.clip_id,p.revision,p.account_id,p.state,p.due,p.approved,p.attempts,p.error,
                      p.external_id,p.url,a.platform,a.label
                   FROM publications p JOIN accounts a ON a.id=p.account_id ORDER BY p.created DESC""")]
            metrics = [unpack(row, "fields") for row in conn.execute(
                """SELECT id,publication_id,recorded,source,fields FROM analytics WHERE id IN
                   (SELECT MAX(id) FROM analytics GROUP BY publication_id)""")]
        integrations = {platform: oauth.enabled(platform, settings) for platform in oauth.PLATFORMS}
        return {"videos": videos, "clips": clips, "jobs": jobs, "accounts": accounts,
                "publications": publications, "integrations": integrations, "metrics": metrics}

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str):
        with store.tx() as conn:
            one(conn, "jobs", job_id)
            return [dict(row) for row in conn.execute(
                "SELECT stamp,progress,message FROM events WHERE job_id=? ORDER BY seq", (job_id,))]

    @app.post("/api/videos/{video_id}/process", status_code=202)
    def process(video_id: str, payload: Processing):
        with store.tx(True) as conn:
            video = unpack(one(conn, "videos", video_id), "info")
            active = conn.execute("""SELECT 1 FROM jobs WHERE video_id=? AND kind='VIDEO'
                                  AND state IN ('QUEUED','RUNNING')""", (video_id,)).fetchone()
            if active:
                raise HTTPException(409, "Já existe uma análise em andamento para este vídeo.")
            config = payload.model_dump()
            tracks = [track["index"] for track in video["info"]["audio"]]
            config["audio_stream"] = tracks[0] if config["audio_stream"] is None else config["audio_stream"]
            if config["audio_stream"] not in tracks:
                raise HTTPException(422, "A faixa de áudio escolhida não existe.")
            if config["webcam"] and min(config["webcam"]["w"] * video["info"]["width"],
                                        config["webcam"]["h"] * video["info"]["height"]) < 16:
                raise HTTPException(422, "A região de webcam deve ter pelo menos 16 pixels em cada eixo.")
            return {"id": store.enqueue(conn, video_id, None, "VIDEO", config)}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        with store.tx(True) as conn:
            job = one(conn, "jobs", job_id)
            if job["state"] not in {"QUEUED", "RUNNING"}:
                raise HTTPException(409, "O processamento já terminou.")
            state = "CANCELLED" if job["state"] == "QUEUED" else "RUNNING"
            conn.execute("UPDATE jobs SET cancel=1,state=?,message=? WHERE id=?",
                         (state, "Cancelamento solicitado.", job_id))
        return {"state": state}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str):
        try:
            with store.tx(True) as conn:
                job = unpack(one(conn, "jobs", job_id), "config")
                if job["state"] not in {"FAILED", "CANCELLED"}:
                    raise HTTPException(409, "A tarefa ainda está ativa ou já terminou com sucesso.")
                clip = job["clip_id"]
                if clip and one(conn, "clips", clip)["revision"] != job["config"]["revision"]:
                    raise HTTPException(409, "Essa revisão já foi substituída.")
                return {"id": store.enqueue(conn, job["video_id"], clip, job["kind"], job["config"])}
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Já existe uma tarefa ativa para este item.") from error

    @app.get("/api/clips/{clip_id}/transcript")
    def transcript(clip_id: str):
        with store.tx() as conn:
            clip = unpack(one(conn, "clips", clip_id), "candidate", "fields")
            cache = conn.execute("SELECT transcript FROM analysis_cache WHERE video_id=? AND cache_key=?",
                                 (clip["video_id"], clip["candidate"]["cache_key"])).fetchone()
            return unpack(cache, "transcript")["transcript"]

    @app.get("/api/clips/{clip_id}/video")
    def clip_file(clip_id: str, download: bool = False):
        with store.tx() as conn:
            clip = one(conn, "clips", clip_id)
        if clip["rendered_revision"] != clip["revision"]:
            raise HTTPException(409, "A edição atual ainda está em renderização.")
        path = settings.file("clips", f"{clip_id}-{clip['revision']}.mp4")
        if not path.is_file():
            raise HTTPException(404, "MP4 indisponível.")
        return FileResponse(path, media_type="video/mp4",
                            filename=f"myclipfactory-{clip_id[:8]}.mp4" if download else None)

    @app.put("/api/clips/{clip_id}", status_code=202)
    def edit_clip(clip_id: str, payload: Edit, revision: int = Query(ge=1)):
        with store.tx(True) as conn:
            clip = unpack(one(conn, "clips", clip_id), "candidate")
            info = unpack(one(conn, "videos", clip["video_id"]), "info")["info"]
            if not 2 <= payload.end - payload.start <= 90 or payload.end > info["duration"]:
                raise HTTPException(422, "Escolha um corte de 2 a 90 segundos dentro do vídeo.")
            if not clip["candidate"]["webcam"] and payload.layout in {"split", "webcam"}:
                raise HTTPException(422, "Este clipe não tem uma região de webcam.")
            if clip["revision"] != revision or current_job(conn, clip_id):
                raise HTTPException(409, "A revisão mudou ou já está em renderização.")
            if payload.corrections:
                cache = conn.execute("SELECT transcript FROM analysis_cache WHERE video_id=? AND cache_key=?",
                                     (clip["video_id"], clip["candidate"]["cache_key"])).fetchone()
                segments = unpack(cache, "transcript")["transcript"]["segments"]
                valid = {s["id"] for s in segments}
                submitted = [c.id for c in payload.corrections]
                if len(submitted) != len(set(submitted)) or not set(submitted) <= valid:
                    raise HTTPException(422, "A correção contém segmentos ausentes ou repetidos.")
            revoke(conn, clip_id)
            new_revision = revision + 1
            fields = dump(payload.model_dump())
            conn.execute("UPDATE clips SET revision=?,fields=?,state='PENDING' WHERE id=?",
                         (new_revision, fields, clip_id))
            conn.execute("INSERT INTO edits VALUES(?,?,?,?)", (clip_id, new_revision, fields, time.time()))
            return {"id": store.enqueue(conn, clip["video_id"], clip_id, "RENDER", {"revision": new_revision})}

    @app.post("/api/clips/{clip_id}/review")
    def review(clip_id: str, payload: Review):
        with store.tx(True) as conn:
            clip = one(conn, "clips", clip_id)
            if clip["revision"] != payload.revision or clip["rendered_revision"] != payload.revision:
                raise HTTPException(409, "Somente a edição atual renderizada pode ser revisada.")
            if payload.decision == "REJECTED":
                revoke(conn, clip_id)
            conn.execute("UPDATE clips SET state=? WHERE id=?", (payload.decision, clip_id))
        return {"state": payload.decision}

    @app.post("/api/oauth/{platform}/start")
    def oauth_start(platform: str):
        return {"url": oauth.start(platform, settings, store)}

    @app.get("/api/oauth/{platform}/callback")
    def oauth_callback(platform: str, state: str = "", code: str = ""):
        if not state or not code or platform not in oauth.PLATFORMS:
            return RedirectResponse("/?oauth=denied", status_code=303)
        try:
            with httpx.Client(timeout=20) as client:
                oauth.connect(platform, state, code, client, settings, store)
        except (PublishError, KeyError, ValueError) as error:
            logger.warning("Conexão OAuth %s falhou: %s", platform, type(error).__name__)
            return RedirectResponse("/?oauth=denied", status_code=303)
        return RedirectResponse("/?oauth=connected", status_code=303)

    @app.get("/api/accounts/{account_id}/creator")
    def creator_info(account_id: str):
        with store.tx() as conn:
            account = dict(one(conn, "accounts", account_id))
        if account["platform"] != "tiktok" or not settings.tiktok_enabled:
            raise HTTPException(422, "A conta não está habilitada para TikTok Direct Post.")
        with httpx.Client(timeout=20) as client:
            creator = tiktok.profile(client, oauth.access(account, client, settings, store))
        if not settings.tiktok_audited:
            creator["privacy"] = [value for value in creator["privacy"] if value == "SELF_ONLY"]
        return creator

    @app.post("/api/clips/{clip_id}/publish", status_code=202)
    def approve_publication(clip_id: str, payload: Publish):
        if not payload.consent:
            raise HTTPException(422, "A publicação exige consentimento explícito para este destino.")
        fields = payload.model_dump(exclude={"account_id", "revision", "consent", "due"})
        with store.tx() as conn:
            account = dict(one(conn, "accounts", payload.account_id))
            clip = unpack(one(conn, "clips", clip_id), "fields")
            if clip["state"] != "APPROVED" or clip["revision"] != payload.revision or clip["rendered_revision"] != payload.revision:
                raise HTTPException(409, "Aprove a versão renderizada atual antes de autorizar um destino.")
        platform = account["platform"]
        fields["duration"] = clip["fields"]["end"] - clip["fields"]["start"]
        if platform == "youtube":
            privacy = fields["privacy"]
            if privacy not in {"private", "unlisted", "public"}:
                raise HTTPException(422, "Selecione a privacidade do YouTube.")
            if privacy != "private" and not settings.youtube_audited:
                raise HTTPException(422, "O aplicativo YouTube deve passar pela auditoria antes de publicar fora de privado.")
        elif platform == "tiktok":
            if not fields["tiktok_terms"]:
                raise HTTPException(422, "Confirme os termos do TikTok antes de autorizar o envio.")
            if settings.tiktok_pull:
                queue.signed_media(settings, "validation")
            elif urlsplit(settings.origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise HTTPException(422, "Para arquivos em servidor, configure PULL_FROM_URL com um domínio verificado.")
            creator = creator_info(payload.account_id)
            tiktok.validate(fields, creator, fields["duration"], settings)
        elif platform == "instagram":
            queue.signed_media(settings, "validation")
        now = time.time()
        due = max(now, payload.due if payload.due is not None else now)
        state = "SCHEDULED" if due > now + 1 else "APPROVED"
        with store.tx(True) as conn:
            current = one(conn, "clips", clip_id)
            if current["state"] != "APPROVED" or current["revision"] != payload.revision or current["rendered_revision"] != payload.revision:
                raise HTTPException(409, "A aprovação mudou durante a preparação da publicação.")
            previous = conn.execute(
                """SELECT id,state,approved FROM publications WHERE clip_id=? AND account_id=?
                   AND (revision=? OR approved IS NOT NULL) ORDER BY approved DESC LIMIT 1""",
                (clip_id, payload.account_id, payload.revision),
            ).fetchone()
            if previous and previous["approved"] is not None:
                return {"id": previous["id"], "state": previous["state"]}
            identifier = previous["id"] if previous else uid()
            conn.execute(
                """INSERT INTO publications(id,clip_id,account_id,revision,fields,state,due,approved,created)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET fields=excluded.fields,
                     state=excluded.state,due=excluded.due,approved=excluded.approved,error=NULL""",
                (identifier, clip_id, payload.account_id, payload.revision, dump(fields), state, due, now, now),
            )
        return {"id": identifier, "state": state}

    @app.post("/api/publications/{publication_id}/cancel")
    def cancel_publication(publication_id: str):
        with store.tx(True) as conn:
            row = one(conn, "publications", publication_id)
            if row["state"] not in {"APPROVED", "SCHEDULED"}:
                raise HTTPException(409, "Só é possível retirar a autorização antes de iniciar o envio.")
            conn.execute("""UPDATE publications SET state='PENDING',approved=NULL,
                          error='Autorização retirada pelo operador.' WHERE id=?""", (publication_id,))
        return {"state": "PENDING"}

    @app.get("/api/publications/{publication_id}/metrics")
    def metrics_history(publication_id: str):
        with store.tx() as conn:
            one(conn, "publications", publication_id)
            return [unpack(row, "fields") for row in conn.execute(
                "SELECT recorded,source,fields FROM analytics WHERE publication_id=? ORDER BY recorded DESC",
                (publication_id,))]

    @app.post("/api/publications/{publication_id}/metrics", status_code=201)
    def add_metrics(publication_id: str, metrics: Metrics):
        with store.tx(True) as conn:
            row = one(conn, "publications", publication_id)
            if row["state"] != "PUBLISHED":
                raise HTTPException(409, "Registre métricas após confirmar a publicação.")
            conn.execute("INSERT INTO analytics(publication_id,recorded,source,fields) VALUES(?,?,'MANUAL',?)",
                         (publication_id, time.time(), dump(metrics.model_dump())))
        return {"state": "RECORDED"}

    @app.get("/media/{publication_id}")
    @app.head("/media/{publication_id}")
    def public_media(publication_id: str, e: int, s: str):
        now = int(time.time())
        if not settings.key or not now <= e <= now + 3600 or not hmac.compare_digest(
            queue.media_signature(settings, publication_id, e), s
        ):
            raise HTTPException(403, "Assinatura inválida ou expirada.")
        with store.tx() as conn:
            row = conn.execute("""SELECT p.clip_id,p.revision FROM publications p
                JOIN accounts a ON a.id=p.account_id WHERE p.id=? AND p.approved IS NOT NULL
                AND a.platform IN ('instagram','tiktok') AND p.state IN ('PROCESSING','PUBLISHED')""", (publication_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Mídia indisponível.")
        path = settings.file("clips", f"{row['clip_id']}-{row['revision']}.mp4")
        if not path.is_file():
            raise HTTPException(404, "Mídia indisponível.")
        return FileResponse(path, media_type="video/mp4")

    return app


app = create_app()
