"""Drive the real MAS consent UI with a container-local trusted authority."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


class ApprovalBrowser:
    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password

    async def __aenter__(self):
        db = Path(os.environ["HOME"]) / ".pki/nssdb"
        db.mkdir(parents=True, exist_ok=True)
        if not (db / "cert9.db").exists():
            await asyncio.to_thread(
                subprocess.run,
                ["certutil", "-N", "--empty-password", "-d", f"sql:{db}"],
                check=True,
                timeout=15,
            )
        await asyncio.to_thread(
            subprocess.run,
            [
                "certutil",
                "-A",
                "-d",
                f"sql:{db}",
                "-n",
                "MEDRE lab",
                "-t",
                "C,,",
                "-i",
                "/lab/config/ca.crt",
            ],
            check=True,
            timeout=15,
        )
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(args=["--no-sandbox"])
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.page.set_default_timeout(10000)
        return self

    async def approve(self, url: str, code: str | None = None) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "auth.lab.test":
            raise ValueError("Approval escaped the lab authorization server")
        await self.page.goto(url)
        deadline = asyncio.get_running_loop().time() + 75
        while asyncio.get_running_loop().time() < deadline:
            page = self.page
            if urlsplit(page.url).hostname != "auth.lab.test":
                raise ValueError("Browser consent redirected outside the lab")
            if await page.locator('input[name="username"]').is_visible():
                await page.locator('input[name="username"]').fill(self.username)
            if await page.locator('input[name="password"]').is_visible():
                await page.locator('input[name="password"]').fill(self.password)
                await page.locator('button[type="submit"]').first.click()
                continue
            code_input = page.locator('input[name="user_code"], input[name="code"]')
            if code and await code_input.is_visible():
                await code_input.fill(code)
                await page.locator('button[type="submit"]').first.click()
                continue
            confirm = page.locator('input[name="confirm_device"]')
            if await confirm.is_visible():
                await confirm.check()
            consent = page.locator('button[name="action"][value="consent"]')
            if await consent.is_visible():
                await consent.click()
                return
            approve = page.get_by_role(
                "button",
                name=re.compile(r"^(Approve|Continue|Confirm|Authorize|Allow)", re.I),
            )
            if await approve.count():
                await approve.first.click()
                # UIA completion may redirect to a static success page.
                continue
            body = await page.locator("body").inner_text()
            if re.search(
                r"(success|approved|authorised|authorized|can close|you can now)",
                body,
                re.I,
            ):
                return
            await asyncio.sleep(0.2)
        # Browser snapshots can contain transient codes; keep them private.
        await self.page.screenshot(path="/lab/results/browser-failure.png")
        Path("/lab/results/browser-failure.html").write_text(await self.page.content())
        raise TimeoutError("MAS browser consent did not complete")

    async def __aexit__(self, *_):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()
