import re

from .base import PublishError, Result, bearer, caption, data, remote_id, request, require_https

API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


def profile(client, token):
    items = data(request(client, "GET", f"{API}/channels", headers=bearer(token),
                         params={"part": "id,snippet", "mine": "true"})).get("items", [])
    if len(items) != 1:
        raise PublishError("O OAuth deve identificar um único canal do YouTube.")
    return remote_id(items[0]["id"]), items[0]["snippet"]["title"]


def status(client, token, video_id, privacy):
    items = data(request(client, "GET", f"{API}/videos", headers=bearer(token),
                         params={"part": "processingDetails,status", "id": video_id})).get("items", [])
    if not items:
        return Result(wait=25)
    remote = items[0]
    processing = remote.get("processingDetails", {}).get("processingStatus")
    upload = remote.get("status", {}).get("uploadStatus")
    if processing in {"failed", "terminated"} or upload in {"failed", "rejected", "deleted"}:
        raise PublishError("O YouTube rejeitou o vídeo durante o processamento.")
    if processing == "succeeded" or upload == "processed":
        if remote.get("status", {}).get("privacyStatus") != privacy:
            raise PublishError(f"YouTube concluiu o vídeo {video_id}, mas aplicou outra privacidade. Confira no Studio.")
        return Result(video_id, f"https://www.youtube.com/shorts/{video_id}")
    return Result(wait=25)


def publish(client, token, account, path, fields, checkpoint, save, settings, signed_url):
    if checkpoint.get("video"):
        return status(client, token, checkpoint["video"], fields["privacy"])
    size = path.stat().st_size
    headers = bearer(token)
    session = checkpoint.get("session")
    if not session:
        response = request(
            client, "POST", UPLOAD, allowed=(200, 201),
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=headers | {"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
            json={"snippet": {"title": fields["title"], "description": caption(fields, "description", 5000),
                              "tags": [t.lstrip("#") for t in fields["hashtags"]], "categoryId": "20"},
                  "status": {"privacyStatus": fields["privacy"]}},
        )
        session = require_https(response.headers.get("Location", ""), {"googleapis.com", "youtube.com"})
        save(session=session)
    else:
        session = require_https(session, {"googleapis.com", "youtube.com"})
    probe = request(client, "PUT", session, allowed=(200, 201, 308, 404, 410),
                    headers=headers | {"Content-Length": "0", "Content-Range": f"bytes */{size}"})
    if probe.status_code in (404, 410):
        raise PublishError("Sessão do YouTube expirada. Confira o Studio antes de reenviar.")
    if probe.status_code != 308:
        video_id = remote_id(data(probe).get("id"))
        save(video=video_id)
        return Result()
    matched = re.fullmatch(r"bytes=0-(\d+)", probe.headers.get("Range", ""))
    offset = int(matched[1]) + 1 if matched else 0
    with path.open("rb") as file:
        file.seek(offset)
        while offset < size:
            chunk = file.read(8 * 1024**2)
            last = offset + len(chunk) - 1
            response = request(client, "PUT", session, allowed=(200, 201, 308),
                               headers=headers | {"Content-Range": f"bytes {offset}-{last}/{size}",
                                                  "Content-Type": "video/mp4"},
                               content=chunk)
            if response.status_code != 308:
                video_id = remote_id(data(response).get("id"))
                save(video=video_id)
                return Result()
            matched = re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", ""))
            if not matched or int(matched[1]) != last:
                raise PublishError("O YouTube recebeu parte do bloco. Retomada agendada.", retry=True)
            offset = last + 1
    raise PublishError("O YouTube ainda não confirmou o upload.", retry=True)
