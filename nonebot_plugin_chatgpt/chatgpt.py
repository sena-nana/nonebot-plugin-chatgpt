import uuid
from typing import Any, Dict, Optional
from contextlib import asynccontextmanager
from nonebot import get_driver
from nonebot.log import logger
from nonebot.utils import escape_tag, run_sync
from typing_extensions import Self
from playwright.async_api import async_playwright, Route, Page

driver = get_driver()
try:
    import ujson as json
except ModuleNotFoundError:
    import json


SESSION_TOKEN_KEY = "__Secure-next-auth.session-token"


class Chatbot:
    def __init__(
        self,
        *,
        token: str = "",
        account: str = "",
        password: str = "",
        api: str = "https://chat.openai.com/",
        proxies: Optional[str] = None,
        timeout: int = 10,
    ) -> None:
        self.session_token = token
        self.account = account
        self.password = password
        self.api_url = api
        self.proxies = proxies
        self.timeout = timeout
        self.content = None
        self.parent_id = None
        self.conversation_id = None
        self.browser = None
        self.is_first_run = True
        self.playwright = async_playwright()
        if self.session_token:
            self.auto_auth = False
        elif self.account and self.password:
            self.auto_auth = True
        else:
            raise ValueError("至少需要配置 session_token 或者 account 和 password")

    async def playwright_start(self):
        """启动浏览器，在插件开始运行时调用"""
        playwright = await self.playwright.start()
        try:
            self.browser = await playwright.firefox.launch(
                headless=True,
                proxy={"server": self.proxies} if self.proxies else None,  # your proxy
            )
        except Exception as e:
            logger.opt(exception=e).error("playwright未安装，请先在shell中运行playwright install")
            return
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) Gecko/20100101 Firefox/{self.browser.version}"
        self.content = await self.browser.new_context(user_agent=ua)
        await self.set_cookie(self.session_token)

    async def set_cookie(self, session_token):
        """设置session_token"""
        await self.content.add_cookies(
            [
                {
                    "name": SESSION_TOKEN_KEY,
                    "value": session_token,
                    "domain": "chat.openai.com",
                    "path": "/",
                }
            ]
        )

    @driver.on_shutdown
    async def playwright_close(self):
        """关闭浏览器"""
        await self.content.close()
        await self.browser.close()
        await self.playwright.__aexit__()

    def __call__(
        self, conversation_id: Optional[str] = None, parent_id: Optional[str] = None
    ) -> Self:
        self.conversation_id = conversation_id[-1] if conversation_id else None
        self.parent_id = parent_id[-1] if parent_id else self.id
        return self

    @property
    def id(self) -> str:
        return str(uuid.uuid4())

    def get_payload(self, prompt: str) -> Dict[str, Any]:
        return {
            "action": "next",
            "messages": [
                {
                    "id": self.id,
                    "role": "user",
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_id,
            "model": "text-davinci-002-render",
        }

    @asynccontextmanager
    async def get_page(self):
        """打开网页，这是一个异步上下文管理器，使用async with调用"""
        page = await self.content.new_page()
        js = "Object.defineProperties(navigator, {webdriver:{get:()=>undefined}});"
        await page.add_init_script(js)
        await page.goto("https://chat.openai.com/chat")
        yield page
        await page.close()

    async def get_chat_response(self, prompt: str) -> str:
        async with self.get_page() as page:
            logger.debug("正在发送请求")
            if self.proxies:
                await self.get_cf_cookies(page)

            async def change_json(route: Route):
                await route.continue_(
                    post_data=json.dumps(self.get_payload(prompt)),
                )

            await self.content.route(
                "https://chat.openai.com/backend-api/conversation", change_json
            )
            if self.is_first_run:
                await page.wait_for_selector(
                    ".btn.flex.justify-center.gap-2.btn-neutral.ml-auto"
                )
                await page.click(".btn.flex.justify-center.gap-2.btn-neutral.ml-auto")
                await page.click(".btn.flex.justify-center.gap-2.btn-neutral.ml-auto")
                await page.click(".btn.flex.justify-center.gap-2.btn-primary.ml-auto")
                self.is_first_run = False
            await page.wait_for_selector("textarea")
            async with page.expect_response(
                "https://chat.openai.com/backend-api/conversation",
                timeout=self.timeout * 1000,
            ) as response_info:
                textarea = page.locator("textarea")
                botton = page.locator("button").last
                for _ in range(3):
                    if await textarea.is_enabled():
                        await textarea.fill(prompt)
                    await page.wait_for_timeout(500)
                    if await botton.is_enabled():
                        await botton.click()
            response = await response_info.value
            if response.status == 429:
                return "请求过多，请放慢速度"
            if response.status == 401:
                return "token失效，请重新设置token"
            if response.status == 403:
                await self.get_cf_cookies(page)
                return await self.get_chat_response(prompt)
            if response.status != 200:
                logger.opt(colors=True).error(
                    f"非预期的响应内容: <r>HTTP{response.status}</r> {escape_tag(response.text)}"
                )
                return f"ChatGPT 服务器返回了非预期的内容: HTTP{response.status}\n{response.text}"
            lines = await response.text()
            lines = lines.splitlines()
            data = lines[-4][6:]
            response = json.loads(data)
            self.parent_id = response["message"]["id"]
            self.conversation_id = response["conversation_id"]
            logger.debug("发送请求结束")
        return response["message"]["content"]["parts"][0]

    async def refresh_session(self) -> None:
        logger.debug("正在刷新session")
        if self.auto_auth:
            await self.login()
        else:
            async with self.get_page() as page:
                cf = page.locator("#challenging-running")
                if await cf.count():
                    await self.get_cf_cookies(page)
                async with page.expect_request(
                    "https://chat.openai.com/api/auth/session"
                ) as session:
                    page.goto("https://chat.openai.com/chat")
                request = await session.value
                response = await request.response()
            if response.status != 200:
                logger.opt(colors=True).error(
                    f"刷新会话失败: <r>HTTP{response.status}</r> {escape_tag(await response.text())}"
                )
            logger.debug("刷新会话成功")

    @run_sync
    def login(self) -> None:
        from OpenAIAuth.OpenAIAuth import OpenAIAuth

        auth = OpenAIAuth(self.account, self.password, bool(self.proxies), self.proxies)  # type: ignore
        try:
            auth.begin()
        except Exception as e:
            if str(e) == "Captcha detected":
                logger.error("不支持验证码, 请使用 session token")
            raise e
        if not auth.access_token:
            logger.error("ChatGPT 登陆错误!")
        if auth.session_token:
            self.session_token = auth.session_token
        elif possible_tokens := auth.session.cookies.get(SESSION_TOKEN_KEY):
            if len(possible_tokens) > 1:
                self.session_token = possible_tokens[0]
            else:
                try:
                    self.session_token = possible_tokens
                except Exception as e:
                    logger.opt(exception=e).error("ChatGPT 登陆错误!")
        else:
            logger.error("ChatGPT 登陆错误!")

    @staticmethod
    async def get_cf_cookies(page: Page) -> None:
        logger.debug("正在获取cf cookies")
        for i in range(20):
            button = page.get_by_role("button", name="Verify you are human")
            if await button.count():
                await button.click()
            label = page.locator("label span")
            if await label.count():
                await label.click()
            await page.wait_for_timeout(1000)
            textarea = page.locator("textarea")
            if await textarea.count():
                break
        else:
            logger.error("cf cookies获取失败")
        logger.debug("cf cookies获取成功")
