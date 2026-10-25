from __future__ import annotations

import json
import asyncio
import logging
from time import time
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict
from datetime import datetime, timedelta, timezone
from contextlib import suppress, asynccontextmanager
from typing import Any, Literal, Final, NoReturn, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL

from translate import _
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    CaptchaRequired,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    chunk,
    timestamp,
    create_nonce,
    task_wrapper,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    MAX_CHANNELS,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
)

if TYPE_CHECKING:
    from utils import Game
    from gui import LoginForm
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation


logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, *args):
        # skip whitespace check
        obj, end = self.raw_decode(s)
        return obj


SAFE_LOADS = lambda s: json.loads(s, cls=SkipExtraJsonDecoder)


async def _oauth_json(response, stage: str) -> JsonType:
    """Reject unexpected OAuth responses without logging credentials or raw bodies."""
    try:
        body = await response.json()
    except (ValueError, UnicodeError, aiohttp.ContentTypeError):
        raise LoginException(f"Twitch {stage}: HTTP {response.status}, response is not JSON") from None
    if not isinstance(body, dict):
        raise LoginException(f"Twitch {stage}: HTTP {response.status}, response is not an object")
    return body


def _oauth_error(body: JsonType) -> str:
    # Never echo arbitrary remote strings: error bodies may contain sensitive data.
    raw = body.get('error') or body.get('message')
    known = {'invalid client', 'invalid_client', 'authorization_pending', 'slow_down',
             'expired_token', 'access_denied', 'invalid_grant', 'invalid_request',
             'invalid_scope', 'unsupported_grant_type'}
    return raw if isinstance(raw, str) and raw in known else 'unrecognized OAuth error'


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def invalidate(self, *, delete_cookies: bool = False) -> None:
        self._delattrs("access_token", "user_id")
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")
        if delete_cookies:
            session = self._twitch._session
            if session is not None:
                jar = cast(aiohttp.CookieJar, session.cookie_jar)
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
        )
        self._logged_in.clear()
        self._twitch.gui.help._invalidate_button.config(state="disabled")

    async def _oauth_login(self) -> str:
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": "",  # no scopes needed
        }
        while True:
            try:
                now = datetime.now(timezone.utc)
                async with self._twitch.request(
                    "POST", "https://id.twitch.tv/oauth2/device", headers=headers, data=payload
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH"
                    # }
                    response_json = await _oauth_json(response, 'device authorization')
                    if response.status != 200:
                        raise LoginException(
                            f"Twitch device authorization: HTTP {response.status}, {_oauth_error(response_json)}"
                        )
                    for field in ('device_code', 'user_code', 'verification_uri'):
                        if not isinstance(response_json.get(field), str) or not 1 <= len(response_json[field]) <= 4096:
                            raise LoginException(f"Twitch device authorization: HTTP 200, missing or invalid {field}")
                    for field, limit in (('interval', 300), ('expires_in', 86400)):
                        if type(response_json.get(field)) is not int or not 1 <= response_json[field] <= limit:
                            raise LoginException(f"Twitch device authorization: HTTP 200, missing or invalid {field}")
                    device_code: str = response_json["device_code"]
                    user_code: str = response_json["user_code"]
                    interval: int = response_json["interval"]
                    verification_uri: URL = URL(response_json["verification_uri"])
                    if verification_uri.scheme != 'https' or verification_uri.host not in ('www.twitch.tv', 'twitch.tv', 'id.twitch.tv'):
                        raise LoginException('Twitch device authorization: untrusted verification URL')
                    expires_at = now + timedelta(seconds=response_json["expires_in"])

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(verification_uri, user_code, expires_at=expires_at)

                token_payload = {
                    "client_id": self._twitch._client_type.CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await self._twitch.gui.coro_unless_closed(asyncio.sleep(interval))
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=token_payload,
                        invalidate_after=expires_at,
                    ) as response:
                        # Pending is expected; denied/expired/rate-limited are not the same.
                        if response.status != 200:
                            failure = await _oauth_json(response, 'token polling')
                            error = _oauth_error(failure)
                            if error == "authorization_pending":
                                continue
                            if error == "slow_down" or response.status == 429:
                                interval = min(interval + 5, 300)
                                continue
                            if error == "expired_token":
                                raise RequestInvalid()
                            raise LoginException(f"Twitch token polling: HTTP {response.status}, {error}")
                        response_json = await _oauth_json(response, 'token polling')
                        if not isinstance(response_json.get('access_token'), str) or not 1 <= len(response_json['access_token']) <= 4096:
                            raise LoginException('Twitch token polling: HTTP 200, missing or invalid access_token')
                        # {
                        #     "access_token": "40 chars [A-Za-z0-9]",
                        #     "refresh_token": "40 chars [A-Za-z0-9]",
                        #     "scope": [...],
                        #     "token_type": "bearer"
                        # }
                        self.access_token = cast(str, response_json["access_token"])
                        return self.access_token
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

    async def _login(self) -> str:
        logger.info("Login flow started")
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type

        token_kind: str = ''
        use_chrome: bool = False
        payload: JsonType = {
            # username and password are added later
            # "username": str,
            # "password": str,
            # client ID to-be associated with the access token
            "client_id": client_info.CLIENT_ID,
            "undelete_user": False,  # purpose unknown
            "remember_me": True,  # persist the session via the cookie
            # "authy_token": str,  # 2FA token
            # "twitchguard_code": str,  # email code
            # "captcha": str,  # self-fed captcha
            # 'force_twitchguard': False,  # force email code confirmation
        }

        while True:
            login_data = await login_form.ask_login()
            payload["username"] = login_data.username
            payload["password"] = login_data.password
            # reinstate the 2FA token, if present
            payload.pop("authy_token", None)
            payload.pop("twitchguard_code", None)
            if login_data.token:
                # if there's no token kind set yet, and the user has entered a token,
                # we can immediately assume it's an authenticator token and not an email one
                if not token_kind:
                    token_kind = "authy"
                if token_kind == "authy":
                    payload["authy_token"] = login_data.token
                elif token_kind == "email":
                    payload["twitchguard_code"] = login_data.token

            # use fancy headers to mimic the twitch android app
            headers = {
                "Accept": "application/vnd.twitchtv.v3+json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Client-Id": client_info.CLIENT_ID,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "passport.twitch.tv",
                "User-Agent": client_info.USER_AGENT,
                "X-Device-Id": self.device_id,
                # "X-Device-Id": ''.join(random.choices('0123456789abcdef', k=32)),
            }
            async with self._twitch.request(
                "POST", "https://passport.twitch.tv/login", headers=headers, json=payload
            ) as response:
                login_response: JsonType = await response.json(loads=SAFE_LOADS)

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code: int = login_response["error_code"]
                logger.info(f"Login error code: {error_code}")
                if error_code == 1000:
                    logger.info("1000: CAPTCHA is required")
                    use_chrome = True
                    break
                elif error_code in (2004, 3001):
                    logger.info("3001: Login failed due to incorrect username or password")
                    gui_print(_("login", "incorrect_login_pass"))
                    if error_code == 2004:
                        # invalid username
                        login_form.clear(login=True)
                    login_form.clear(password=True)
                    continue
                elif error_code in (
                    3012,  # Invalid authy token
                    3023,  # Invalid email code
                ):
                    logger.info("3012/23: Login failed due to incorrect 2FA code")
                    if error_code == 3023:
                        token_kind = "email"
                        gui_print(_("login", "incorrect_email_code"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "incorrect_twofa_code"))
                    login_form.clear(token=True)
                    continue
                elif error_code in (
                    3011,  # Authy token needed
                    3022,  # Email code needed
                ):
                    # 2FA handling
                    logger.info("3011/22: 2FA token required")
                    # user didn't provide a token, so ask them for it
                    if error_code == 3022:
                        token_kind = "email"
                        gui_print(_("login", "email_code_required"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "twofa_code_required"))
                    continue
                elif error_code >= 5000:
                    # Special errors, usually from Twitch telling the user to "go away"
                    # We print the code out to inform the user, and just use chrome flow instead
                    # {
                    #     "error_code":5023,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client is not supported for this feature"
                    # }
                    # {
                    #     "error_code":5027,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client blocked from this operation"
                    # }
                    gui_print(_("login", "error_code").format(error_code=error_code))
                    logger.info(str(login_response))
                    use_chrome = True
                    break
                else:
                    ext_msg = str(login_response)
                    logger.info(ext_msg)
                    raise LoginException(ext_msg)
            # Success handling
            if "access_token" in login_response:
                self.access_token = cast(str, login_response["access_token"])
                logger.info("Access token granted")
                login_form.clear()
                break

        if use_chrome:
            # await self._chrome_login()
            raise CaptchaRequired()

        if hasattr(self, "access_token"):
            return self.access_token
        raise LoginException("Login flow finished without setting the access token")

    def headers(self, *, user_agent: str = '', gql: bool = False) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        # if hasattr(self, "client_version"):
            # headers["Client-Version"] = self.client_version
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
            client_info: ClientInfo = self._twitch._client_type
        if not self._hasattrs("device_id"):
            async with self._twitch.request(
                "GET", client_info.CLIENT_URL, headers=self.headers()
            ) as response:
                page_html = await response.text("utf8")
                assert page_html is not None
            #     match = re.search(r'twilightBuildID="([-a-z0-9]+)"', page_html)
            # if match is None:
            #     raise MinerException("Unable to extract client_version")
            # self.client_version = match.group(1)
            # doing the request ends up setting the "unique_id" value in the cookie
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            self.device_id = cookie["unique_id"].value
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for client_mismatch_attempt in range(2):
                for invalid_token_attempt in range(2):
                    cookie = jar.filter_cookies(client_info.CLIENT_URL)
                    if "auth-token" not in cookie:
                        self.access_token = await self._oauth_login()
                        cookie["auth-token"] = self.access_token
                    elif not hasattr(self, "access_token"):
                        logger.info("Restoring session from cookie")
                        self.access_token = cookie["auth-token"].value
                    # validate the auth token, by obtaining user_id
                    async with self._twitch.request(
                        "GET",
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {self.access_token}"}
                    ) as response:
                        if response.status == 401:
                            # the access token we have is invalid - clear the cookie and reauth
                            logger.info("Restored session is invalid")
                            assert client_info.CLIENT_URL.host is not None
                            jar.clear_domain(client_info.CLIENT_URL.host)
                            continue
                        elif response.status == 200:
                            validate_response = await response.json()
                            break
                else:
                    raise RuntimeError("Login verification failure (step #2)")
                # ensure the cookie's client ID matches the currently selected client
                if validate_response["client_id"] == client_info.CLIENT_ID:
                    break
                # otherwise, we need to delete the entire cookie file and clear the jar
                logger.info("Cookie client ID mismatch")
                jar.clear()
                COOKIES_PATH.unlink(missing_ok=True)
            else:
                raise RuntimeError("Login verification failure (step #1)")
            self.user_id = int(validate_response["user_id"])
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        self._twitch.gui.help._invalidate_button.config(state="normal")
        self._logged_in.set()


class Twitch:
    def __init__(self, settings: Settings, *, gui_factory=None, client_type=None):
        self.settings: Settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._monitor_cache = {}
        self._monitor_last = {}
        self._monitor_classes = {}
        self._monitor_rotation = {}
        self.monitoring = None
        self._monitor_force_priority = False
        self._monitor_force_all = False
        self.discovery = {'source': 'twitch', 'status': 'not_loaded', 'updated_at': None,
                          'count': 0, 'message': '尚未获取活动'}
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._mnt_triggers: deque[datetime] = deque()
        # NOTE: GQL is pretty volatile and breaks everything if one runs into their rate limit.
        # Do not modify the default, safe values.
        self._qgl_limiter = RateLimiter(capacity=5, window=1)
        # Client type, session and auth
        self._client_type: ClientInfo = client_type if client_type is not None else ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        # GUI
        if gui_factory is None:
            from gui import GUIManager
            gui_factory = GUIManager
        self.gui = gui_factory(self)
        # Storing and watching channels
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if (session := self._session) is not None:
            if session.closed:
                raise RuntimeError("Session is closed")
            return session
        # load in cookies
        cookie_jar = aiohttp.CookieJar()
        try:
            if COOKIES_PATH.exists():
                cookie_jar.load(COOKIES_PATH)
        except Exception:
            # if loading in the cookies file ends up in an error, just ignore it
            # clear the jar, just in case
            cookie_jar.clear()
        # create timeouts
        # connection quality mulitiplier determines the magnitude of timeouts
        connection_quality = self.settings.connection_quality
        if connection_quality < 1:
            connection_quality = self.settings.connection_quality = 1
        elif connection_quality > 6:
            connection_quality = self.settings.connection_quality = 6
        timeout = aiohttp.ClientTimeout(
            sock_connect=5*connection_quality,
            total=10*connection_quality,
        )
        # create session, limited to 50 connections at maximum
        connector = aiohttp.TCPConnector(limit=50)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=cookie_jar,
            headers={"User-Agent": self._client_type.USER_AGENT},
        )
        return self._session

    async def shutdown(self) -> None:
        start_time = time()
        self.stop_watching()
        if self._watching_task is not None:
            self._watching_task.cancel()
            self._watching_task = None
        if self._mnt_task is not None:
            self._mnt_task.cancel()
            self._mnt_task = None
        # stop websocket, close session and save cookies
        await self.websocket.stop(clear_topics=True)
        if self._session is not None:
            cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
            # clear empty cookie entries off the cookies file before saving
            # NOTE: Unfortunately, aiohttp provides no easy way of clearing empty cookies,
            # so we need to access the private '_cookies' attribute for this.
            for cookie_key, cookie in list(cookie_jar._cookies.items()):
                if not cookie:
                    del cookie_jar._cookies[cookie_key]
            cookie_jar.save(COOKIES_PATH)
            await self._session.close()
            self._session = None
        self._drops.clear()
        self.channels.clear()
        self.inventory.clear()
        self._auth_state.clear()
        self.wanted_games.clear()
        self._monitor_cache.clear()
        self._monitor_last.clear()
        self._monitor_classes.clear()
        self._monitor_rotation.clear()
        self._mnt_triggers.clear()
        # wait at least half a second + whatever it takes to complete the closing
        # this allows aiohttp to safely close the session
        await asyncio.sleep(start_time + 0.5 - time())

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is not State.EXIT:
            # prevent state changing once we switch to exit state
            self._state = state
        self._state_change.set()

    def state_change(self, state: State) -> abc.Callable[[], None]:
        # this is identical to change_state, but defers the call
        # perfect for GUI usage
        return partial(self.change_state, state)

    def close(self):
        """
        Called when the application is requested to close by the user,
        usually by the console or application window being closed.
        """
        self.change_state(State.EXIT)

    def prevent_close(self):
        """
        Called when the application window has to be prevented from closing, even after the user
        closes it with X. Usually used solely to display tracebacks from the closing sequence.
        """
        self.gui.prevent_close()

    def print(self, message: str):
        """
        Can be used to print messages within the GUI.
        """
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """
        Saves the application state.
        """
        self.gui.save(force=force)
        self.settings.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """
        Return a priority number for a given channel.

        0 has the highest priority.
        Higher numbers -> lower priority.
        MAX_INT (a really big number) signifies the lowest possible priority.
        """
        if (
            (game := channel.game) is None  # None when OFFLINE or no game set
            or game not in self.wanted_games  # we don't care about the played game
        ):
            return MAX_INT
        return self.wanted_games.index(game)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    async def run(self):
        if self.settings.dump:
            with open(DUMP_PATH, 'w', encoding="utf8"):
                # replace the existing file with an empty one
                pass
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise RequestException(_("login", "unexpected_content")) from exc

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        auth_state = await self.get_auth()
        await self.websocket.start()
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_loop())
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.IDLE:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.tray.change_icon("idle")
                self.gui.status.update(_("gui", "status", "idle"))
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                self.gui.tray.change_icon("maint")
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                self.gui.set_games(set(campaign.game for campaign in self.inventory
                                       if not campaign.expired and not campaign.finished
                                       and any(d.required_minutes > 0 and d.required_subs == 0
                                               and not d.is_claimed for d in campaign.drops)))
                # Save state on every inventory fetch
                self.save()
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                await drop.claim()
                # figure out which games we want
                self.wanted_games.clear()
                exclude = self.settings.exclude
                priority = self.settings.priority
                priority_mode = self.settings.priority_mode
                priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                # sorted_campaigns: list[DropsCampaign] = list(self.inventory)
                sorted_campaigns: list[DropsCampaign] = self.inventory
                if not priority_only:
                    if priority_mode is PriorityMode.ENDING_SOONEST:
                        sorted_campaigns.sort(key=lambda c: c.ends_at)
                    elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                        sorted_campaigns.sort(key=lambda c: c.availability)
                sorted_campaigns.sort(
                    key=lambda c: (
                        priority.index(c.game.name) if c.game.name in priority else MAX_INT
                    )
                )
                for campaign in sorted_campaigns:
                    game: Game = campaign.game
                    if (
                        game not in self.wanted_games  # isn't already there
                        # and isn't excluded by list or priority mode
                        and game.name not in exclude
                        and (not priority_only or game.name in priority)
                        # and can be progressed within the next hour
                        and campaign.can_earn_within(next_hour)
                    ):
                        # non-excluded games with no priority are placed last, below priority ones
                        self.wanted_games.append(game)
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                # Keep current subscriptions until the replacement allocation is ready.
                self.change_state(State.CHANNELS_FETCH)
            elif self._state is State.CHANNELS_FETCH:
                await self._refresh_monitored_channels()
                self.change_state(State.CHANNEL_SWITCH)
            elif self._state is State.CHANNEL_SWITCH:
                if self.settings.dump:
                    self.gui.close()
                    continue
                self.gui.status.update(_("gui", "status", "switching"))
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self.get_priority):
                        if self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None and self.can_watch(watching_channel):
                    # otherwise, continue watching what we had before
                    self.gui.status.update(
                        _("status", "watching").format(channel=watching_channel.name)
                    )
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    logger.info('当前没有符合观看条件的频道，进入等待；频道状态变化或活动刷新后重新评估')
                    self.print(_("status", "no_channel"))
                    self.change_state(State.IDLE)
                del new_watching, selected_channel, watching_channel
            elif self._state is State.RESTART:
                raise ReloadRequest()
            elif self._state is State.EXIT:
                self.gui.tray.change_icon("pickaxe")
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    async def _watch_sleep(self, delay: float) -> None:
        # we use wait_for here to allow an asyncio.sleep-like that can be ended prematurely
        self._watching_restart.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)

    @task_wrapper(critical=True)
    async def _watch_loop(self) -> NoReturn:
        interval: float = WATCH_INTERVAL.total_seconds()
        while True:
            channel: Channel = await self.watching_channel.get()
            if not channel.online:
                # if the channel isn't online anymore, we stop watching it
                self.stop_watching()
                continue
            # logger.log(CALL, f"Sending watch payload to: {channel.name}")
            last_sent: float = time()
            succeeded: bool = await channel.send_watch()
            if not succeeded:
                logger.log(CALL, f"Watch requested failed for channel: {channel.name}")
            # wait ~15 seconds for a progress update
            await asyncio.sleep(15 - min(time() - last_sent, 15))
            if self.gui.progress.minute_almost_done():
                # If the previous update was more than ~60s ago, and the progress tracker
                # isn't counting down anymore, that means Twitch has temporarily
                # stopped reporting drop's progress. To ensure the timer keeps at least somewhat
                # accurate time, we can use GQL to query for the current drop,
                # or even "pretend" mining as a last resort option.
                handled: bool = False

                # Solution 1: use GQL to query for the currently mined drop status
                try:
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                except GQLException:
                    drop_data = None
                if drop_data is not None:
                    gql_drop: TimedDrop | None = self._drops.get(drop_data["dropID"])
                    if gql_drop is not None and gql_drop.can_earn(channel):
                        gql_drop.update_minutes(drop_data["currentMinutesWatched"])
                        drop_text: str = (
                            f"{gql_drop.name} ({gql_drop.campaign.game}, "
                            f"{gql_drop.current_minutes}/{gql_drop.required_minutes})"
                        )
                        logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                        handled = True

                # Solution 2: If GQL fails, figure out which campaign we're most likely mining
                # right now, and then bump up the minutes on it's drops
                if not handled:
                    if (active_campaign := self.get_active_campaign(channel)) is not None:
                        active_campaign.bump_minutes(channel)
                        # NOTE: This usually gets overwritten below
                        drop_text = f"Unknown drop ({active_campaign.game})"
                        if (active_drop := active_campaign.first_drop) is not None:
                            active_drop.display()
                            drop_text = (
                                f"{active_drop.name} ({active_drop.campaign.game}, "
                                f"{active_drop.current_minutes}/{active_drop.required_minutes})"
                            )
                        logger.log(CALL, f"Drop progress from active search: {drop_text}")
                        handled = True
                    else:
                        logger.log(CALL, "No active drop could be determined")
            await self._watch_sleep(interval - min(time() - last_sent, interval))

    async def _refresh_monitored_channels(self):
        from channel_monitor import (GamePool, allocate_channels, rank_candidates,
                                     rotate_candidates, search_due, OTHER_GAMES)
        now = time()
        force_priority, force_all = self._monitor_force_priority, self._monitor_force_all
        self._monitor_force_priority = self._monitor_force_all = False
        horizon = datetime.now(timezone.utc) + timedelta(hours=1)
        eligible = [c for c in self.inventory if c.game in self.wanted_games
                    and c.can_earn_within(horizon)]
        game_campaigns = {}
        for campaign in eligible:
            game_campaigns.setdefault(campaign.game, []).append(campaign)
        priority = [g for name in self.settings.priority for g in game_campaigns if g.name == name]
        other = sorted((g for g in game_campaigns if g not in priority),
                       key=lambda g: (min(c.ends_at for c in game_campaigns[g]), g.id))[:OTHER_GAMES]
        games = priority + other
        game_ids = {g.id for g in games}
        for key in list(self._monitor_cache):
            if key not in game_ids:
                self._monitor_cache.pop(key, None)
                self._monitor_last.pop(key, None)
                self._monitor_classes.pop(key, None)
        watching = self.watching_channel.get_with_default(None)
        watching = watching if watching is not None and self.can_watch(watching) else None
        selected_before = self.gui.channels.get_selection()
        pools = []
        for game in games:
            is_priority = game in priority
            campaigns = game_campaigns[game]
            acl = {channel.id: self.channels.get(channel.id, channel)
                   for campaign in campaigns for channel in campaign.allowed_channels}
            unrestricted = any(not c.allowed_channels for c in campaigns)
            small_acl = bool(acl) and len(acl) <= 10 and not unrestricted
            signature = (is_priority, tuple(sorted(acl)), unrestricted)
            last = self._monitor_last.get(game.id)
            if signature != self._monitor_classes.get(game.id):
                last = None
            if force_all or (force_priority and is_priority) or search_due(last, now, is_priority):
                self.gui.status.update(f'搜索直播频道：{game.name}')
                logger.info('按游戏搜索「%s」（%s，周期 %s 分钟）', game.name,
                            '优先列表' if is_priority else '列表外', 15 if is_priority else 60)
                limit = 40 if is_priority else 10
                if small_acl:
                    sample = list(acl.values())
                else:
                    previous = [self.channels[c.id] for c in self._monitor_cache.get(game.id, [])
                                if c.id in self.channels and c.id in acl and self.channels[c.id].online]
                    keep = previous[:limit // 2]
                    offset = self._monitor_rotation.get(game.id, 0)
                    remaining = [c for c in acl.values() if c.id not in {v.id for v in keep}]
                    sample = keep + rotate_candidates(remaining, offset, limit - len(keep))
                    self._monitor_rotation[game.id] = offset + max(1, limit - len(keep))
                try:
                    # Existing limiter still gates every GQL request. Bound each
                    # discovery so one unavailable game cannot block all monitoring.
                    if sample:
                        await asyncio.wait_for(self.bulk_check_online(sample), timeout=60)
                    candidates = sample
                    if unrestricted:
                        found = await asyncio.wait_for(self.get_live_streams(
                            game, limit=20 if is_priority else 5, drops_enabled=True), timeout=60)
                        candidates = candidates + found
                    if watching is not None and (watching.id in acl or (unrestricted and watching.game == game)):
                        candidates = [watching] + candidates
                    self._monitor_cache[game.id] = rank_candidates(candidates,
                        lambda c: self.can_watch(c) and (c.game == game or game.is_special()),
                                                                 small_acl=small_acl)
                    self._monitor_last[game.id] = time()
                    self._monitor_classes[game.id] = signature
                    logger.info('游戏「%s」指定名单 %d 个，本轮检查 %d 个，候选 %d 个，在线 %d 个',
                                game.name, len(acl), len(sample), len(self._monitor_cache[game.id]),
                                sum(c.online for c in self._monitor_cache[game.id]))
                except (asyncio.TimeoutError, RequestException, GQLException, MinerException) as exc:
                    logger.warning('游戏「%s」频道搜索失败（%s），保留上一轮候选，下个周期重试',
                                   game.name, type(exc).__name__)
                    # Avoid a failing endpoint being retried on every boundary event.
                    self._monitor_last[game.id] = time()
                    self._monitor_classes[game.id] = signature
            candidates = self._monitor_cache.get(game.id, [])
            pools.append(GamePool(game.id, game.name, is_priority, candidates, small_acl))
        latest_watching = self.watching_channel.get_with_default(None)
        watching = latest_watching if latest_watching is not None and self.can_watch(latest_watching) else None
        ordered, assigned = allocate_channels(pools, MAX_CHANNELS, watching)
        # Use the same live object for shared channels, so websocket updates don't
        # leave stale clones in other games' cached candidate lists.
        canonical = {c.id: c for c in ordered}
        if watching is not None:
            canonical[watching.id] = watching
        for game_id, candidates in self._monitor_cache.items():
            self._monitor_cache[game_id] = [canonical.get(c.id, c) for c in candidates]
        ordered = [canonical[c.id] for c in ordered]
        keep_ids = set(canonical)
        removed = [c for c in self.channels.values() if c.id not in keep_ids]
        topics = [WebsocketTopic.as_str('Channel', kind, c.id)
                  for c in removed for kind in ('StreamState', 'StreamUpdate')]
        self.websocket.remove_topics(topics)
        for channel in removed:
            channel.remove()
        self.channels.clear()
        self.gui.channels.clear()
        summaries = []
        for pool in pools:
            ids = assigned[pool.game_id]
            summaries.append({'game': pool.name, 'priority': pool.priority,
                              'tracked': len(ids), 'channels': [str(i) for i in ids],
                              'search_interval_minutes': 15 if pool.priority else 60})
            logger.info('监听配额「%s」：保留 %d 个，在线 %d 个；%s', pool.name, len(ids),
                        sum(canonical[i].online for i in ids),
                        '小型指定名单完整保留' if pool.small_acl and len(ids) == len(pool.channels)
                        else '按游戏配额分配')
        for channel in ordered:
            channel.monitor_games = [p.name for p in pools if channel.id in assigned[p.game_id]]
            self.channels[channel.id] = channel
            channel.display(add=True)
        self.websocket.add_topics([WebsocketTopic('Channel', kind, c.id, callback)
            for c in ordered for kind, callback in (('StreamState', self.process_stream_state),
                                                    ('StreamUpdate', self.process_stream_update))])
        if selected_before is not None and selected_before.id in self.channels and hasattr(self.gui.channels, 'selected'):
            self.gui.channels.selected = self.channels[selected_before.id]
        if watching is not None and watching.id in self.channels:
            self.watch(self.channels[watching.id], update_status=False)
        else:
            self.stop_watching()
        self.monitoring = {'tracked': len(ordered), 'limit': MAX_CHANNELS, 'games': summaries,
                           'priority_interval_minutes': 15, 'other_interval_minutes': 60}
        logger.info('频道分配完成：监听 %d / %d 个，优先列表 %d 个游戏，列表外 %d 个游戏；其余列表外游戏待下轮截止时间排序',
                    len(ordered), MAX_CHANNELS, len(priority), len(other))

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        now = datetime.now(timezone.utc)
        next_period = now + timedelta(hours=1)
        next_priority = now + timedelta(minutes=15)
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = min(next_period, next_priority)
            while self._mnt_triggers and self._mnt_triggers[0] <= next_trigger:
                next_trigger = self._mnt_triggers.popleft()
            trigger_type: str = "Reload" if next_trigger == next_period else "Cleanup"
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if now >= next_priority:
                next_priority = now + timedelta(minutes=15)
                self._monitor_force_priority = True
                logger.info('15 分钟优先列表搜索周期到达，重新分配监听频道；列表外沿用小时缓存')
            if next_trigger != next_period:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self.change_state(State.CHANNELS_CLEANUP)
        # this triggers a restart of this task every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self._monitor_force_all = True
        self.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        # exit early if stream is offline
        if not channel.online:
            return False
        for campaign in self.inventory:
            if (
                campaign.can_earn(channel)  # let the campaign do the "special games" check
                and (
                    # limit watching to the games the user wants
                    channel.game is not None
                    and channel.drops_enabled
                    and channel.game in self.wanted_games
                    # let the campaign ignore all channel-related checks
                    or campaign.game.is_special()
                )
            ):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.
        """
        if not self.can_watch(channel):
            return False
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None or not self.can_watch(watching_channel):
            return True
        channel_order = self.get_priority(channel)
        watching_order = self.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order < watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        if update_status:
            logger.info('选择观看频道：%s；游戏：%s', channel.name, channel.game.name if channel.game else '未知')
        self.gui.tray.change_icon("active")
        self.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        if update_status:
            status_text = _("status", "watching").format(channel=channel.name)
            self.print(status_text)
            self.gui.status.update(status_text)

    def stop_watching(self):
        self.gui.clear_drop()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        self._watching_restart.set()

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger.warning(f"Unknown stream state: {msg_type}")

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'check_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if self.should_switch(channel):
                    # we can watch the channel, and we should
                    self.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch(channel)
                else:
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            watching_channel = self.watching_channel.get_with_default(None)
            # check if the watching channel was the one updated
            if watching_channel is not None and watching_channel == channel:
                # NOTE: In these cases, channel was the watching channel
                if not self.can_watch(channel):
                    # we can't watch it anymore
                    if stream_after is None:
                        # Channel going OFFLINE
                        self.print(_("status", "goes_offline").format(channel=channel.name))
                    else:
                        # Channel stays ONLINE, but we can't watch it anymore
                        logger.info(
                            f"{channel.name} status has been updated, switching... "
                            f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                            f"{stream_after.drops_enabled and '✔' or '❌'})"
                        )
                    self.change_state(State.CHANNEL_SWITCH)
                else:
                    # Channel stays ONLINE, and we can still watch it - no change
                    pass
            # NOTE: In these cases, it wasn't the watching channel
            elif stream_after is None:
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                # Channel stays ONLINE, but has been updated
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
                if self.should_switch(channel):
                    # ... and we should watch it
                    self.watch(channel)
        channel.display()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        drop_id: str = message["data"]["drop_id"]
        drop: TimedDrop | None = self._drops.get(drop_id)
        watching_channel: Channel | None = self.watching_channel.get_with_default(None)
        if msg_type == "drop-claim":
            if drop is None:
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return
            drop.update_claim(message["data"]["drop_instance_id"])
            campaign = drop.campaign
            await drop.claim()
            drop.display()
            # About 4-20s after claiming the drop, next drop can be started
            # by re-sending the watch payload. We can test for it by fetching the current drop
            # via GQL, and then comparing drop IDs.
            await asyncio.sleep(4)
            if watching_channel is not None:
                for attempt in range(8):
                    context = await self.gql_request(
                        GQL_QUERIES["CurrentDrop"].with_variables(
                            {"channelID": str(watching_channel.id)}
                        )
                    )
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                    if drop_data is None or drop_data["dropID"] != drop.id:
                        break
                    await asyncio.sleep(2)
            if campaign.can_earn(watching_channel):
                self.restart_watching()
            else:
                self.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"
        logger.log(CALL, f"Drop update from websocket: {drop_text}")
        if drop is not None and drop.can_earn(self.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] in (
                "user_drop_reward_reminder_notification",  # drop confirmation
                "quests_viewer_reward_campaign_earned_emote",  # emote confirmation
                # badge confirmation?
            ):
                self.change_state(State.INVENTORY_FETCH)
                await self.gql_request(
                    GQL_QUERIES["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, *, invalidate_after: datetime | None = None, **kwargs
    ) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        logger.debug(f"Request: ({method=}, {url=}, {kwargs=})")
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            elif (
                invalidate_after is not None
                # account for the expiration landing during the request
                and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
            ):
                raise RequestInvalid()
            try:
                response: aiohttp.ClientResponse | None = None
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                assert response is not None
                logger.debug(f"Response: {response.status}: {response}")
                if response.status < 500:
                    # pre-read the response to avoid getting errors outside of the context manager
                    raw_response = await response.read()  # noqa
                    yield response
                    return
                self.print(_("error", "site_down").format(seconds=round(delay)))
            except aiohttp.ClientConnectorCertificateError:
                # for a case where SSL verification fails
                raise
            except (
                aiohttp.ClientConnectionError, asyncio.TimeoutError, aiohttp.ClientPayloadError
            ):
                # connection problems, retry
                if backoff.steps > 1:
                    # just so that quick retries that sometimes happen, aren't shown
                    self.print(
                        _("error", "no_connection").format(seconds=round(delay), url=str(url))
                    )
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=delay)

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list[GQLOperation]) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops: GQLOperation | list[GQLOperation]
    ) -> JsonType | list[JsonType]:
        gql_logger.debug(f"GQL Request: {ops}")
        backoff = ExponentialBackoff(maximum=60)
        # Use a flag to retry the request a single time, if a specific set of errors is encountered
        single_retry: bool = True
        for delay in backoff:
            async with self._qgl_limiter:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True),
                ) as response:
                    response_json: JsonType | list[JsonType] = await response.json()
            gql_logger.debug(f"GQL Response: {response_json}")
            orig_response = response_json
            if isinstance(response_json, list):
                response_list = response_json
            else:
                response_list = [response_json]
            force_retry: bool = False
            for response_json in response_list:
                # GQL error handling
                if "errors" in response_json:
                    for error_dict in response_json["errors"]:
                        if "message" in error_dict:
                            if (
                                single_retry
                                and error_dict["message"] in (
                                    "service error",
                                    "PersistedQueryNotFound",
                                )
                            ):
                                logger.error(
                                    f"Retrying a {error_dict['message']} for "
                                    f"{response_json['extensions']['operationName']}"
                                )
                                single_retry = False
                                if delay < 5:
                                    # overwrite the delay if too short
                                    delay = 5
                                force_retry = True
                                break
                            elif error_dict["message"] == "server error":
                                # nullify the key the error path points to
                                data_dict: JsonType = response_json["data"]
                                path: list[str] = error_dict.get("path", [])
                                for key in path[:-1]:
                                    data_dict = data_dict[key]
                                data_dict[path[-1]] = None
                                break
                            elif (
                                error_dict["message"] in (
                                    "service timeout",
                                    "request cancelled",
                                    "service unavailable",
                                    "context deadline exceeded",
                                )
                            ):
                                force_retry = True
                                break
                    else:
                        raise GQLException(response_json['errors'])
                # Other error handling
                elif "error" in response_json:
                    raise GQLException(
                        f"{response_json['error']}: {response_json['message']}"
                    )
                if force_retry:
                    break
            else:
                return orig_response
            await asyncio.sleep(delay)
        raise RuntimeError("Retry loop was broken")

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        merged = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            if in_primary and key in secondary_data:
                vp = primary_data[key]
                vs = secondary_data[key]
                if not isinstance(vp, type(vs)) or not isinstance(vs, type(vp)):
                    raise MinerException("Inconsistent merge data")
                if isinstance(vp, dict):  # both are dicts
                    merged[key] = self._merge_data(vp, vs)
                else:
                    # use primary value
                    merged[key] = vp
            elif in_primary:
                merged[key] = primary_data[key]
            else:  # in campaigns only
                merged[key] = secondary_data[key]
        return merged

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        response_list: list[JsonType] = await self.gql_request(
            [
                GQL_QUERIES["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {
            (campaign_data := response_json["data"]["user"]["dropCampaign"])["id"]: campaign_data
            for response_json in response_list
        }
        return self._merge_data(campaign_ids, fetched_data)

    async def fetch_inventory(self) -> None:
        status_update = self.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # fetch in-progress campaigns (inventory)
        response = await self.gql_request(GQL_QUERIES["Inventory"])
        inventory: JsonType = response["data"]["currentUser"]["inventory"]
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []
        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: timestamp(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }
        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}
        # fetch general available campaigns data (campaigns)
        response = await self.gql_request(GQL_QUERIES["Campaigns"])
        available_list: list[JsonType] = response["data"]["currentUser"]["dropCampaigns"] or []
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c["status"] in applicable_statuses  # that are currently not expired
        }
        self.discovery = {'source': 'twitch', 'status': 'ok',
                          'updated_at': datetime.now(timezone.utc).isoformat(),
                          'count': len(available_campaigns), 'message': '活动目录来自 Twitch'}
        if self._client_type is ClientType.SMARTBOX:
            from campaign_catalog import fetch_catalog, merge_account_campaigns
            try:
                public, updated = await fetch_catalog(self.settings.proxy)
                # Twitch account data always wins. Never import catalog self/progress.
                inventory_data = merge_account_campaigns(inventory_data, public)
                self.discovery = {'source': 'public_catalog', 'status': 'ok', 'updated_at': updated,
                                  'count': len(public),
                                  'message': '活动目录由公开数据补全；账号关联、进度和领取以 Twitch 为准'}
                logger.info(f'公开活动目录已加载：{len(public)} 个活动；账号数据仍来自 Twitch')
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                # Do not repeat arbitrary external response bodies in logs.
                self.discovery = {'source': 'public_catalog', 'status': 'error', 'updated_at': None,
                                  'count': 0, 'message': '完整活动目录暂不可用，当前仅显示 Twitch 已返回的活动；请刷新重试'}
                logger.warning(f'公开活动目录不可用（{type(exc).__name__}）；当前列表可能不完整')
        # fetch detailed data for each campaign, in chunks
        status_update(_("gui", "status", "fetching_campaigns"))
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
            for campaigns_chunk in chunk(available_campaigns.items(), 20)
            if self._client_type is not ClientType.SMARTBOX
        ]
        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                chunk_campaigns_data = await coro
                # merge the inventory and campaigns datas together
                inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in fetch_campaigns_tasks:
                task.cancel()
            raise
        # filter out invalid campaigns
        for campaign_id in list(inventory_data.keys()):
            if inventory_data[campaign_id]["game"] is None:
                del inventory_data[campaign_id]

        if self.settings.dump:
            # dump the campaigns data to the dump file
            with open(DUMP_PATH, 'a', encoding="utf8") as file:
                # we need to pre-process the inventory dump a little
                dump_data: JsonType = deepcopy(inventory_data)
                for campaign_data in dump_data.values():
                    # replace ACL lists with a simple text description
                    if (
                        campaign_data["allow"]
                        and campaign_data["allow"].get("isEnabled", True)
                        and campaign_data["allow"]["channels"]
                    ):
                        # simply count the channels included in the ACL
                        campaign_data["allow"]["channels"] = (
                            f"{len(campaign_data['allow']['channels'])} channels"
                        )
                    # replace drop instance IDs, so they don't include user IDs
                    for drop_data in campaign_data["timeBasedDrops"]:
                        if "self" in drop_data and drop_data["self"]["dropInstanceID"]:
                            drop_data["self"]["dropInstanceID"] = "..."
                json.dump(dump_data, file, indent=4, sort_keys=True)
                file.write("\n\n")  # add 2x new line spacer
                json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

        # use the merged data to create campaign objects
        from account_links import attach_link_evidence
        matched_links = attach_link_evidence(inventory_data, inventory.get('gameEventDrops'))
        logger.info('账号关联入口检查完成：%d 个活动匹配 Twitch 奖励记录；活动资格仍以账号接口为准', matched_links)
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self, campaign_data, claimed_benefits)
            for campaign_data in inventory_data.values()
        ]
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)

        self._drops.clear()
        self.gui.inv.clear()
        self.inventory.clear()
        self._mnt_triggers.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        # add the campaigns to the internal inventory
        for campaign in campaigns:
            self._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            self.inventory.append(campaign)
            self._campaigns[campaign.id] = campaign
        # concurrently add the campaigns into the GUI
        # NOTE: this fetches pictures from the CDN, so might be slow without a cache
        status_update(
            _("gui", "status", "adding_campaigns").format(counter=f"(0/{len(campaigns)})")
        )
        add_campaign_tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self.gui.inv.add_campaign(campaign))
            for campaign in campaigns
        ]
        try:
            for i, coro in enumerate(asyncio.as_completed(add_campaign_tasks), start=1):
                await coro
                status_update(
                    _("gui", "status", "adding_campaigns").format(
                        counter=f"({i}/{len(campaigns)})"
                    )
                )
                # this is needed here explicitly, because cache reads from disk don't raise this
                if self.gui.close_requested:
                    raise ExitRequest()
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in add_campaign_tasks:
                task.cancel()
            raise
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.popleft()
        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._mnt_task is not None and not self._mnt_task.done():
            self._mnt_task.cancel()
        self._mnt_task = asyncio.create_task(self._maintenance_task())

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        if not self.wanted_games:
            return None
        watching_channel = self.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        campaigns: list[DropsCampaign] = []
        for campaign in self.inventory:
            if campaign.can_earn(watching_channel):
                campaigns.append(campaign)
        if campaigns:
            campaigns.sort(key=lambda c: c.remaining_minutes)
            return campaigns[0]
        return None

    async def get_live_streams(
        self, game: Game, *, limit: int = 20, drops_enabled: bool = True
    ) -> list[Channel]:
        filters: list[str] = []
        if drops_enabled:
            filters.append("DROPS_ENABLED")
        try:
            response = await self.gql_request(
                GQL_QUERIES["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": filters,
                    },
                })
            )
        except GQLException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        if "game" in response["data"]:
            return [
                Channel.from_directory(
                    self, stream_channel_data["node"], drops_enabled=drops_enabled
                )
                for stream_channel_data in response["data"]["game"]["streams"]["edges"]
                if stream_channel_data["node"]["broadcaster"] is not None
            ]
        return []

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        """
        Utilize batch GQL requests to check ONLINE status for a lot of channels at once.
        Also handles the drops_enabled check (if enabled).
        """
        acl_streams_map: dict[int, JsonType] = {}
        stream_gql_ops: list[GQLOperation] = [channel.stream_gql for channel in channels]
        if not stream_gql_ops:
            # shortcut for nothing to process
            # NOTE: Have to do this here, becase "channels" can be any iterable
            return
        stream_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
            asyncio.create_task(self.gql_request(stream_gql_chunk))
            for stream_gql_chunk in chunk(stream_gql_ops, 20)
        ]
        try:
            for coro in asyncio.as_completed(stream_gql_tasks):
                response_list: list[JsonType] = await coro
                for response_json in response_list:
                    channel_data: JsonType = response_json["data"]["user"]
                    if channel_data is not None:
                        acl_streams_map[int(channel_data["id"])] = channel_data
        except Exception:
            # asyncio.as_completed doesn't cancel tasks on errors
            for task in stream_gql_tasks:
                task.cancel()
            raise
        # for all channels with an active stream, check the available drops as well
        acl_available_drops_map: dict[int, list[JsonType]] = {}
        if self.settings.available_drops_check:
            available_gql_ops: list[GQLOperation] = [
                GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)})
                for channel_id, channel_data in acl_streams_map.items()
                if channel_data["stream"] is not None  # only do this for ONLINE channels
            ]
            available_gql_tasks: list[asyncio.Task[list[JsonType]]] = [
                asyncio.create_task(self.gql_request(available_gql_chunk))
                for available_gql_chunk in chunk(available_gql_ops, 20)
            ]
            try:
                for coro in asyncio.as_completed(available_gql_tasks):
                    response_list = await coro
                    for response_json in response_list:
                        available_info: JsonType = response_json["data"]["channel"]
                        acl_available_drops_map[int(available_info["id"])] = (
                            available_info["viewerDropCampaigns"] or []
                        )
            except Exception:
                # asyncio.as_completed doesn't cancel tasks on errors
                for task in available_gql_tasks:
                    task.cancel()
                raise
        for channel in channels:
            channel_id = channel.id
            if channel_id not in acl_streams_map:
                continue
            channel_data = acl_streams_map[channel_id]
            if channel_data["stream"] is None:
                continue
            available_drops: list[JsonType] = acl_available_drops_map.get(channel_id, [])
            channel.external_update(channel_data, available_drops)
