# RT.Backend - OAuth

from typing import TypedDict, Callable, Optional, Union, Literal, Sequence, Dict
from types import SimpleNamespace

from sanic.exceptions import SanicException, ServiceUnavailable
from sanic.response import redirect
from discord import User

from asyncio import AbstractEventLoop
from ujson import loads, dumps
from functools import wraps
from random import randint
from time import time
import reprypt
import aiohttp

from .typed import TypedSanic, TypedBot, CoroutineFunction
from .backend import Request


class CookieData(TypedDict):
    user_id: str
    name: str


class TypedRequestContext(SimpleNamespace):
    user: Optional[User]


class DiscordOAuth:
    "DiscordのOAuth認証の処理を手軽に作るためのクラスです。"

    BASE = "https://discord.com/api/v8/"
    bot: Optional[TypedBot] = None
    loop: Optional[AbstractEventLoop] = None

    class TypedRequest(Request):
        ctx: TypedRequestContext

    def __init__(
        self, app: TypedSanic, client_id: str,
        client_secret: str, secret_key: str = str(time() / randint(2, 100)) * 2
    ):
        self.client_id, self.client_secret = client_id, client_secret
        self.app, self.secret_key = app, secret_key
        self.redirects: Dict[str, str] = {}
        self._session = None
        self.app.ctx.tasks.append(
            lambda app: (
                setattr(self, "bot", app.ctx.bot)
                and setattr(self, "loop", app.loop)
            )
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                loop=self.loop, json_serialize=dumps, raise_for_status=True
            )
        return self._session

    def get_redirect_oauth(self, url: str) -> Optional[str]:
        "リダイレクト許可リストからOAuthのURLを取得します。"
        return self.redirects.get(url)

    def reset_oauth(self, url: str) -> None:
        "指定されたリダイレクトのOAuthのURLをリセットします。"
        self.redirects[url] = None

    def reset_all_oauth(self) -> None:
        "リダイレクト許可リストにあるOAuthのURLをリセットします。"
        for key in list(self.redirects.keys()):
            self.reset_oauth(key)

    async def get_url(
        self, redirect_url: str, scope: Sequence[str], state: Optional[str] = None
    ) -> str:
        """OAuthログイン用のURLを取得します。  
        もしキャッシュがあるならそのURLが使用されます。"""
        if (url := self.redirects.get(redirect_url)) is None:
            params = {
                "response_type": "code",
                "scope": "%20".join(scope),
                "client_id": self.client_id,
                "redirect_uri": redirect_url
            }
            async with self.session.get(f"{self.BASE}oauth2/authorize", params=params) as r:
                url = (url := str(r.url))[:(slash := url.find("?"))] \
                    + url[slash:].replace("/", r"%2F")
        return f'{url}{f"&state={state}" if state else ""}'

    async def get_token(self, code: str, callback_url: str) -> dict:
        "OAuthから渡されたコードからTOKENを取得します。"
        async with self.session.post(
            f"{self.BASE}oauth2/token", data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url
            }, headers={
                "Content-Type": "application/x-www-form-urlencoded"
            }
        ) as r:
            data = await r.json(loads=loads)
        return data

    async def get_userdata(self, token: str) -> User:
        "ユーザーデータをTOKENから取得します。"
        async with self.session.get(
            f"{self.BASE}users/@me", headers={
                "Authorization": f"Bearer {token}"
            }
        ) as r:
            return User(state=self.bot._connection, data=await r.json(loads=loads))

    async def get_user_cookie(self, cookie: str) -> Optional[User]:
        "クッキーからユーザーデータを取得します。"
        try:
            return await self.bot.fetch_user(
                int(loads(reprypt.decrypt(cookie, self.secret_key))["id"])
            )
        except reprypt.DecryptError:
            return None

    def encrypt(self, data: CookieData, **kwargs) -> str:
        "渡されたクッキーデータを暗号化します。"
        return reprypt.encrypt(dumps(data), self.secret_key, **kwargs)

    def make_base_url(self, request: Request) -> str:
        "RequestからベースのURLを作ります。"
        return f"{request.scheme}://{request.host}"

    def make_url(self, request: Request, path: str) -> str:
        "pathとrequestからURLを作ります。"
        return f"{self.make_base_url(request)}{path}"

    def state_generator(self, request: Request) -> str:
        "`require_login`の引数`state_generator`のデフォルトです。"
        return reprypt.encrypt(
            f"{request.host}{request.ip}", self.secret_key,
            converter=reprypt.convert_hex
        )

    def state_checker(self, request: Request, state: str) -> bool:
        "`require_login`の引数`stage_checker`のデフォルトです。"
        try:
            return f"{request.host}{request.ip}" == reprypt.decrypt(
                state, self.secret_key, converter=reprypt.convert_hex
            )
        except reprypt.DecryptError:
            return False

    def _wrap_route(self, func, force, scope, state_generator, state_checker):
        # RouteをOAuthログイン付きのものにする関数です。
        @wraps(func)
        async def new_route(request: Request, *args, **kwargs):
            mode = "normal"

            if self.bot is None:
                raise ServiceUnavailable("まだ起動準備中なので処理を続行できませんでした。")

            if (data := request.cookies.get("session", None)) or force:
                # もし既にクッキーがあるまたは強制モードならログインはパスする。
                request.ctx.user = await self.get_user_cookie(data) if data else None
            elif request.args:
                # もしcodeがあるならログイン後の可能性があるのでログイン後の処理する。
                if state_generator:
                    # もしstate_generatorが設定されているならstateがあっているかを確認してあっていないのならエラーする。
                    if not state_checker(request, request.args.get("state", "")):
                        raise SanicException(
                            "あなたのクエリパラメータの`state`が間違えているので正常に処理ができませんでした。OAuth認証をやり直してください。",
                            403
                        )
                # ユーザーデータを取得する。
                try:
                    request.ctx.user = await self.get_userdata(
                        (await self.get_token(
                            request.args.get("code"), self.make_url(request, request.path)
                        ))["access_token"]
                    )
                except aiohttp.client_exceptions.ClientResponseError as e:
                    raise SanicException(
                        "ユーザーデータの取得に失敗しました。ログイン情報が古かったから発生したエラーの可能性があります。" \
                        f"お手数ですがもう一度ログインしてください。ErrorCode:{e}", 400
                    )
                else:
                    mode = "write-cookie"
            else:
                # もしログインをしていないのならログインURLにリダイレクトさせる。
                redirect_url = self.make_url(request, request.path)

                if state_generator:
                    state = state_generator(request)
                else:
                    state = None
                return redirect(await self.get_url(redirect_url, scope, state))

            response = await func(request, *args, **kwargs)

            if mode == "write-cookie":
                # もしログイン後でクッキーを書く必要があるのならクッキーを書き込んでおく。
                response.cookies["session"] = self.encrypt(
                    {
                        "id": request.ctx.user.id, "name": request.ctx.user.name
                    }
                )

            return response
        return new_route

    def require_login(
        self, force: bool = False, scope: Sequence[str] = ("identify",),
        state_generator: Union[
            Callable[[Request], str], Literal["default"], None
        ] = "default",
        state_checker: Union[
            Callable[[Request], bool], Literal["default"], None
        ] = "default"
    ) -> Callable[..., CoroutineFunction]:
        "Discordログインを必要とするものにつけるデコレータです。"
        state_generator = self.state_generator if state_generator == "default" else None
        state_checker = self.state_checker if state_checker == "default" else None
        def decorator(func):
            return self._wrap_route(
                func, force, scope, state_generator, state_checker
            )
        return decorator