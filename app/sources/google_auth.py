"""Shared Google service-account auth (§4): GOOGLE_SA_JSON_B64 -> bearer tokens.

One Credentials instance per scope tuple, cached in-process; refreshed when
expired (Google access tokens live ~1h).
"""
import base64
import json

from google.auth.transport.requests import Request as GARequest
from google.oauth2 import service_account

from .. import config

_creds: dict[tuple[str, ...], service_account.Credentials] = {}


def get_creds(scopes: list[str]) -> service_account.Credentials:
    """Valid (refreshed) credentials for `scopes`; use `.token` for the bearer."""
    key = tuple(scopes)
    creds = _creds.get(key)
    if creds is None:
        info = json.loads(base64.b64decode(config.env("GOOGLE_SA_JSON_B64")))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=list(scopes)
        )
        _creds[key] = creds
    if not creds.valid:
        # ponytail: sync refresh inside async — rare (1/hour), single user
        creds.refresh(GARequest())
    return creds
