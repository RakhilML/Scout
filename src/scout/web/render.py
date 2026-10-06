"""Pages that build their text with JavaScript, read through a headless browser (optional)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from scout.errors import ConfigError, RenderError


class Renderer(Protocol):
    def render(self, url: str) -> str:
        """The page's HTML after its scripts ran; RenderError when that fails."""
        ...

    def close(self) -> None: ...


def make_renderer(spec: str, *, timeout: float, user_agent: str = "") -> Renderer | None:
    """The renderer SCOUT_RENDER names: none (empty or "off"), or "playwright"."""
    if spec in ("", "off"):
        return None
    if spec == "playwright":
        return PlaywrightRenderer(timeout=timeout, user_agent=user_agent)
    raise ConfigError(f"SCOUT_RENDER must be off or playwright, not {spec!r}")


class PlaywrightRenderer:
    """Headless Chromium through Playwright. Playwright's sync API must stay on the thread that
    started it, so one thread of its own runs the browser, reused for every page, while the
    fetcher's threads hand it URLs."""

    def __init__(self, *, timeout: float = 20.0, user_agent: str = "") -> None:
        try:
            import playwright.sync_api  # noqa: F401  (checked now, used on the browser thread)
        except ImportError as exc:
            raise ConfigError(
                "rendering pages needs Playwright: pip install 'scout[render]', then "
                "playwright install chromium"
            ) from exc
        self._timeout_ms = int(timeout * 1000)
        self._user_agent = user_agent or None
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        self._playwright: Any = None
        self._browser: Any = None

    def render(self, url: str) -> str:
        return self._thread.submit(self._render, url).result()

    def close(self) -> None:
        self._thread.submit(self._stop).result()
        self._thread.shutdown()

    def _render(self, url: str) -> str:
        from playwright.sync_api import Error as PlaywrightError

        try:
            if self._browser is not None and not self._browser.is_connected():
                self._stop()  # the browser died: start a new one
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch()
            context = self._browser.new_context(user_agent=self._user_agent, accept_downloads=False)
            try:
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
                html: str = page.content()
                return html
            finally:
                context.close()
        except PlaywrightError as exc:
            raise RenderError(str(exc).splitlines()[0]) from exc

    def _stop(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = self._playwright = None
