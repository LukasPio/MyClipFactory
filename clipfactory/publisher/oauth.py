import json
import os
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken

from . import instagram, tiktok, youtube
from .base import PublishError, data, remote_id, request
from ..store import dump, uid


PLATFORMS = {
    "youtube": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
    },
    "tiktok": {
        "authorize": "https://www.tiktok.com/v2/auth/authorize/",
        "token": "https://open.tiktokapis.com/v2/oauth/token/",
        "scope": "video.publish",
    },
    "instagram": {
        "authorize": "https://www.instagram.com/oauth/authorize",
        "token": "https://api.instagram.com/oauth/access_token",
        "scope": "instagram_business_basic,instagram_business_content_publishing",
    },
}


def vault(settings):
    try:
        return Fernet(settings.key.encode())
    except (ValueError, TypeError) as error:
        raise PublishError("Configure uma chave Fernet válida em MCF_TOKEN_KEY.") from error


def credentials(platform):
    if platform not in PLATFORMS:
        raise PublishError("Plataforma desconhecida.")
    prefix = "MCF_" + platform.upper()
    return os.getenv(prefix + "_CLIENT_ID", ""), os.getenv(prefix + "_CLIENT_SECRET", "")


def enabled(platform, settings):
    return all(credentials(platform)) and bool(settings.key) and (platform != "tiktok" or settings.tiktok_enabled)


def start(platform, settings, store):
    if not enabled(platform, settings):
        raise PublishError("A integração OAuth desta plataforma ainda não foi configurada.")
    vault(settings)
    with store.tx(True) as conn:
        now = time.time()
        state = uid() + uid()
        conn.execute("DELETE FROM oauth_states WHERE expires<?", (now,))
        conn.execute("INSERT INTO oauth_states VALUES(?,?,?)", (state, platform, now + 600))
    client_id, _ = credentials(platform)
    definition = PLATFORMS[platform]
    key = "client_key" if platform == "tiktok" else "client_id"
    query = {key: client_id, "response_type": "code", "state": state, "scope": definition["scope"],
             "redirect_uri": f"{settings.origin}/api/oauth/{platform}/callback"}
    if platform == "youtube":
        query.update(access_type="offline", prompt="consent")
    if platform == "instagram":
        query.update(enable_fb_login="0", force_authentication="1")
    return definition["authorize"] + "?" + urlencode(query)


def connect(platform, state, code, client, settings, store):
    with store.tx(True) as conn:
        result = conn.execute("DELETE FROM oauth_states WHERE state=? AND platform=? AND expires>=?",
                              (state, platform, time.time()))
        if not result.rowcount:
            raise PublishError("Estado OAuth inválido ou expirado.")
    client_id, secret = credentials(platform)
    key = "client_key" if platform == "tiktok" else "client_id"
    definition = PLATFORMS[platform]
    token = data(request(client, "POST", definition["token"], data={
        key: client_id, "client_secret": secret, "code": code, "grant_type": "authorization_code",
        "redirect_uri": f"{settings.origin}/api/oauth/{platform}/callback",
    }))
    if not token.get("access_token"):
        raise PublishError("A plataforma não autorizou a conexão OAuth.")
    if platform == "instagram":
        token = data(request(client, "GET", "https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": token["access_token"],
        }))
        external_id, label = instagram.profile(client, token["access_token"], settings)
    elif platform == "youtube":
        external_id, label = youtube.profile(client, token["access_token"])
    else:
        creator = tiktok.profile(client, token["access_token"])
        external_id, label = remote_id(token.get("open_id")), creator["nickname"] or creator["username"]
    with store.tx(True) as conn:
        conn.execute(
            """INSERT INTO accounts(id,platform,external_id,label,token) VALUES (?,?,?,?,?)
               ON CONFLICT(platform,external_id) DO UPDATE SET token=excluded.token,label=excluded.label""",
            (uid(), platform, external_id, label, encrypt(token, settings)),
        )


def encrypt(token, settings):
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    return vault(settings).encrypt(dump(token).encode()).decode()


def access(account, client, settings, store):
    cipher = vault(settings)
    try:
        token = json.loads(cipher.decrypt(account["token"].encode()))
    except InvalidToken as error:
        raise PublishError("Credencial inacessível. Reconecte a conta com a chave atual.") from error
    if token["expires_at"] > time.time() + 90:
        return token["access_token"]
    platform = account["platform"]
    if platform == "instagram":
        refreshed = data(request(client, "GET", "https://graph.instagram.com/refresh_access_token",
                                 params={"grant_type": "ig_refresh_token", "access_token": token["access_token"]}))
    else:
        client_id, secret = credentials(platform)
        key = "client_key" if platform == "tiktok" else "client_id"
        if not token.get("refresh_token"):
            raise PublishError("A sessão OAuth expirou. Reconecte a conta.")
        refreshed = data(request(client, "POST", PLATFORMS[platform]["token"], data={
            key: client_id, "client_secret": secret, "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        }))
    if not refreshed.get("access_token"):
        raise PublishError("A plataforma não renovou o OAuth. Reconecte a conta.")
    replacement = encrypt(token | refreshed, settings)
    with store.tx(True) as conn:
        conn.execute("UPDATE accounts SET token=? WHERE id=? AND token=?",
                     (replacement, account["id"], account["token"]))
    return refreshed["access_token"]
