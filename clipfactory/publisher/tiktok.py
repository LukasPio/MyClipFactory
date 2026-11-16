from .base import PublishError, Result, bearer, caption, data, remote_id, request, require_https

API = "https://open.tiktokapis.com/v2/post/publish"


def call(client, token, endpoint, payload=None):
    result = data(request(client, "POST", f"{API}/{endpoint}/", headers=bearer(token), json=payload or {}))
    code = result.get("error", {}).get("code")
    if code != "ok":
        raise PublishError(f"TikTok retornou {code or 'resposta incompleta'}.",
                           retry=code in {"rate_limit_exceeded", "internal_error"})
    return result["data"]


def profile(client, token):
    creator = call(client, token, "creator_info/query")
    return {
        "username": creator.get("creator_username", ""),
        "nickname": creator.get("creator_nickname", ""),
        "privacy": creator.get("privacy_level_options", []),
        "max_duration": creator.get("max_video_post_duration_sec", 0),
        "comment": not creator.get("comment_disabled", True),
        "duet": not creator.get("duet_disabled", True),
        "stitch": not creator.get("stitch_disabled", True),
    }


def validate(fields, creator, duration, settings):
    if not settings.tiktok_enabled:
        raise PublishError("Direct Post está desabilitado. Exporte o MP4 para publicar manualmente.")
    if fields["privacy"] not in creator["privacy"] or not fields["privacy"]:
        raise PublishError("Escolha uma privacidade disponível na conta TikTok.")
    if not settings.tiktok_audited and fields["privacy"] != "SELF_ONLY":
        raise PublishError("O app TikTok sem auditoria só pode usar SELF_ONLY.")
    if not 0 < duration <= creator["max_duration"]:
        raise PublishError("A duração excede o limite da conta TikTok.")
    for ability in ("comment", "duet", "stitch"):
        if fields[f"allow_{ability}"] and not creator[ability]:
            raise PublishError(f"A conta TikTok desabilitou a interação {ability}.")
    if fields["commercial"] != (fields["own_brand"] or fields["branded_content"]):
        raise PublishError("Identifique corretamente o tipo de divulgação comercial.")
    if fields["branded_content"] and fields["privacy"] == "SELF_ONLY":
        raise PublishError("Conteúdo de marca de terceiros não pode usar SELF_ONLY.")


def publish(client, token, account, path, fields, checkpoint, save, settings, signed_url):
    if checkpoint.get("publish_id"):
        remote = call(client, token, "status/fetch", {"publish_id": checkpoint["publish_id"]})
        state = remote.get("status")
        if state == "PUBLISH_COMPLETE":
            identifiers = remote.get("publicaly_available_post_id") or remote.get("publicly_available_post_id") or []
            video_id = remote_id(identifiers[0]) if identifiers else checkpoint["publish_id"]
            user = checkpoint.get("username")
            link = f"https://www.tiktok.com/@{user}/video/{video_id}" if identifiers and user else None
            return Result(video_id, link)
        if state == "FAILED":
            raise PublishError(f"TikTok encerrou o processamento: {remote.get('fail_reason') or 'sem detalhes'}.")
        if state == "PROCESSING_UPLOAD" and checkpoint.get("uncertain_upload"):
            raise PublishError("Upload TikTok interrompido. Confira o TikTok antes de qualquer reenvio.")
        return Result(wait=25)
    if checkpoint.get("pull_started"):
        raise PublishError("Resultado incerto no TikTok. Consulte a conta antes de reenviar o vídeo.")
    creator = profile(client, token)
    validate(fields, creator, fields["duration"], settings)
    size = path.stat().st_size
    chunk_size = min(size, 32 * 1024**2)
    count = size // chunk_size
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    if settings.tiktok_pull:
        source = {"source": "PULL_FROM_URL", "video_url": signed_url()}
        save(pull_started=True)
    response = call(client, token, "video/init", {
        "post_info": {"title": caption(fields, "caption", 2200) or fields["title"],
                      "privacy_level": fields["privacy"], "disable_duet": not fields["allow_duet"],
                      "disable_comment": not fields["allow_comment"], "disable_stitch": not fields["allow_stitch"],
                      "brand_content_toggle": fields["branded_content"], "brand_organic_toggle": fields["own_brand"]},
        "source_info": source,
    })
    publish_id = response.get("publish_id")
    if not isinstance(publish_id, str) or not 1 <= len(publish_id) <= 240:
        raise PublishError("O TikTok não retornou o identificador da publicação.")
    if settings.tiktok_pull:
        save(publish_id=publish_id, username=creator["username"])
        return Result()
    url = require_https(response.get("upload_url", ""), {"tiktokapis.com", "tiktok.com"})
    save(publish_id=publish_id, username=creator["username"], uncertain_upload=True)
    with path.open("rb") as file:
        for index in range(count):
            first = index * chunk_size
            length = size - first if index == count - 1 else chunk_size
            request(client, "PUT", url, allowed=(200, 201, 206),
                    headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {first}-{first+length-1}/{size}"},
                    content=file.read(length))
    save(uncertain_upload=False)
    return Result()
