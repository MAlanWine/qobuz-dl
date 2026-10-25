# Wrapper for Qo-DL Reborn. This is a sligthly modified version
# of qopy, originally written by Sorrow446. All credits to the
# original author.

import hashlib
import logging
import time

import requests

from qobuz_dl.exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppIdError,
    InvalidAppSecretError,
    InvalidQuality,
)
from qobuz_dl.color import GREEN, YELLOW

RESET = "Reset your credentials with 'qobuz-dl -r'"

logger = logging.getLogger(__name__)


class Client:
    def __init__(self, email, pwd, app_id, secrets):
        logger.info(f"{YELLOW}Logging...")
        self.secrets = secrets
        self.id = str(app_id)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
                "X-App-Id": self.id,
            }
        )
        self.base = "https://www.qobuz.com/api.json/0.2/"
        self.sec = None
        self.auth(email, pwd)
        self.cfg_setup()

    def api_call(self, epoint, **kwargs):
        if epoint == "user/login":
            params = {
                "email": kwargs["email"],
                "password": kwargs["pwd"],
                "app_id": self.id,
            }
        elif epoint == "track/get":
            params = {"track_id": kwargs["id"]}
        elif epoint == "album/get":
            params = {"album_id": kwargs["id"]}
        elif epoint == "playlist/get":
            params = {
                "extra": "tracks",
                "playlist_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
            }
        elif epoint == "artist/get":
            params = {
                "app_id": self.id,
                "artist_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
                "extra": "albums",
            }
        elif epoint == "label/get":
            params = {
                "label_id": kwargs["id"],
                "limit": 500,
                "offset": kwargs["offset"],
                "extra": "albums",
            }
        elif epoint == "favorite/getUserFavorites":
            unix = time.time()
            r_sig = "favoritegetUserFavorites" + str(unix) + kwargs.get(
                "sec", self.sec
            )
            r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
            params = {
                "app_id": self.id,
                "user_auth_token": self.uat,
                "type": kwargs.get("me_type") or kwargs.get("type", "albums"),
                "limit": kwargs.get("limit", 50),
                "offset": kwargs.get("offset", 0),
                "request_ts": unix,
                "request_sig": r_sig_hashed,
            }
        elif epoint == "track/getFileUrl":
            unix = time.time()
            track_id = kwargs["id"]
            fmt_id = kwargs["fmt_id"]
            if int(fmt_id) not in (5, 6, 7, 27):
                raise InvalidQuality("Invalid quality id: choose between 5, 6, 7 or 27")
            r_sig = "trackgetFileUrlformat_id{}intentstreamtrack_id{}{}{}".format(
                fmt_id, track_id, unix, kwargs.get("sec", self.sec)
            )
            r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
            params = {
                "request_ts": unix,
                "request_sig": r_sig_hashed,
                "track_id": track_id,
                "format_id": fmt_id,
                "intent": "stream",
            }
        else:
            params = kwargs

        if epoint == "user/login":
            r = self.session.post(self.base + epoint, data=params)
        else:
            r = self.session.get(self.base + epoint, params=params)

        if epoint == "user/login":
            if r.status_code == 401:
                raise AuthenticationError("Invalid credentials.\n" + RESET)
            elif r.status_code == 400:
                raise InvalidAppIdError("Invalid app id.\n" + RESET)
            else:
                logger.info(f"{GREEN}Logged: OK")
        elif (
            epoint in ["track/getFileUrl", "favorite/getUserFavorites"]
            and r.status_code == 400
        ):
            raise InvalidAppSecretError(f"Invalid app secret: {r.json()}.\n" + RESET)

        r.raise_for_status()
        return r.json()

    def auth(self, email, pwd):
        # Direct API login replaced by OAuth+reCAPTCHA.
        # pwd holds the current user_auth_token; we refresh it via extra=partner.
        self.session.headers.update({"X-User-Auth-Token": pwd})
        r = self.session.post(self.base + "user/login", data={"extra": "partner"})
        if r.status_code == 401:
            raise AuthenticationError(
                "Token expired or invalid. Get a fresh token from your browser:\n"
                "  DevTools -> Network -> user/login POST -> Response -> user_auth_token\n"
                "  Then run: qobuz-dl -r  (paste the token as the password)"
            )
        r.raise_for_status()
        data = r.json()
        self.uat = data["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": self.uat})
        self.label = data["user"]["credential"]["parameters"]["short_label"]
        logger.info(f"{GREEN}Membership: {self.label}")
        # Persist refreshed token back to config
        import configparser, os
        config_file = os.path.join(os.environ.get("HOME", ""), ".config", "qobuz-dl", "config.ini")
        if os.path.exists(config_file):
            c = configparser.ConfigParser()
            c.read(config_file)
            if c["DEFAULT"].get("password") != self.uat:
                c["DEFAULT"]["password"] = self.uat
                with open(config_file, "w") as f:
                    c.write(f)
                logger.info(f"{GREEN}Token refreshed and saved.")

    def multi_meta(self, epoint, key, id, type):
        total = 1
        offset = 0
        while total > 0:
            if type in ["tracks", "albums"]:
                j = self.api_call(epoint, id=id, offset=offset, type=type)[type]
            else:
                j = self.api_call(epoint, id=id, offset=offset, type=type)
            if offset == 0:
                yield j
                total = j[key] - 500
            else:
                yield j
                total -= 500
            offset += 500

    def get_album_meta(self, id):
        return self.api_call("album/get", id=id)

    def get_track_meta(self, id):
        return self.api_call("track/get", id=id)

    def get_track_url(self, id, fmt_id):
        return self.api_call("track/getFileUrl", id=id, fmt_id=fmt_id)

    def get_artist_meta(self, id):
        return self.multi_meta("artist/get", "albums_count", id, None)

    def get_plist_meta(self, id):
        return self.multi_meta("playlist/get", "tracks_count", id, None)

    def get_label_meta(self, id):
        return self.multi_meta("label/get", "albums_count", id, None)

    def search_albums(self, query, limit):
        return self.api_call("album/search", query=query, limit=limit)

    def search_artists(self, query, limit):
        return self.api_call("artist/search", query=query, limit=limit)

    def search_playlists(self, query, limit):
        return self.api_call("playlist/search", query=query, limit=limit)

    def search_tracks(self, query, limit):
        return self.api_call("track/search", query=query, limit=limit)

    def get_favorite_albums(self, offset, limit):
        return self.api_call(
            "favorite/getUserFavorites", type="albums", offset=offset, limit=limit
        )

    def get_favorite_tracks(self, offset, limit):
        return self.api_call(
            "favorite/getUserFavorites", type="tracks", offset=offset, limit=limit
        )

    def get_favorite_artists(self, offset, limit):
        return self.api_call(
            "favorite/getUserFavorites", type="artists", offset=offset, limit=limit
        )

    def get_user_playlists(self, limit):
        return self.api_call("playlist/getUserPlaylists", limit=limit)

    def test_secret(self, sec):
        try:
            self.api_call("track/getFileUrl", id=5966783, fmt_id=5, sec=sec)
            return True
        except InvalidAppSecretError:
            return False

    def cfg_setup(self):
        for secret in self.secrets:
            if not secret:
                continue
            if self.test_secret(secret):
                self.sec = secret
                break
        if self.sec is None:
            raise InvalidAppSecretError("Can't find any valid app secret.\n" + RESET)

