from .base import PublishError, Result, bearer, caption, data, remote_id, request


def api(settings):
    return f"https://graph.instagram.com/{settings.instagram_version}"


def profile(client, token, settings):
    value = data(request(client, "GET", f"{api(settings)}/me", headers=bearer(token),
                         params={"fields": "user_id,username"}))
    return remote_id(value.get("user_id")), value["username"]


def publish(client, token, account, path, fields, checkpoint, save, settings, signed_url):
    headers = bearer(token)
    root = api(settings)
    if checkpoint.get("media"):
        value = data(request(client, "GET", f"{root}/{checkpoint['media']}", headers=headers,
                             params={"fields": "id,permalink"}))
        return Result(remote_id(value.get("id")), value.get("permalink"))
    container = checkpoint.get("container")
    if not container:
        value = data(request(client, "POST", f"{root}/{account['external_id']}/media", headers=headers,
                             data={"media_type": "REELS", "video_url": signed_url(),
                                   "caption": caption(fields, "caption", 2200) or fields["title"]}))
        save(container=remote_id(value.get("id")))
        return Result(wait=30)
    value = data(request(client, "GET", f"{root}/{container}", headers=headers, params={"fields": "status_code"}))
    state = value.get("status_code")
    if state in {"ERROR", "EXPIRED"}:
        raise PublishError(f"Instagram encerrou o processamento do contêiner ({state}).")
    if state == "PUBLISHED":
        return Result(complete=True, note="O Instagram confirmou o contêiner. Abra a conta para localizar o link.")
    if state != "FINISHED":
        return Result(wait=35)
    if checkpoint.get("publish_sent"):
        raise PublishError(f"Resultado incerto no Instagram. Confira o contêiner {container} antes de reenviar.")
    save(publish_sent=True)
    try:
        response = request(client, "POST", f"{root}/{account['external_id']}/media_publish",
                           headers=headers, data={"creation_id": container})
        save(media=remote_id(data(response).get("id")))
    except PublishError as error:
        if not error.retry:
            raise
    return Result(wait=30)
