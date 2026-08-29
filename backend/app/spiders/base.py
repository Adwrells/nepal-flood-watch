"""Minimal Scrapy-shaped base class.

Scrapy itself pulls in Twisted and a reactor we do not need for ~5 polite
requests every 12 minutes, so this keeps the ergonomics (name / start_urls /
parse) without the dependency weight.
"""
import httpx

from ..config import settings


class Spider:
    name: str = "spider"
    start_urls: list[str] = []

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # Set False where every URL is required for the result to mean anything.
    partial_ok: bool = True

    async def run(self) -> list[dict]:
        """Fetch every start_url and flatten whatever parse() yields.

        URLs are isolated from each other: one news feed returning 403 must not
        take the other three down with it. self.errors records what failed so
        /api/health can show it instead of silently returning less data.
        """
        items: list[dict] = []
        self.errors: list[str] = []
        for url in self.start_urls:
            try:
                resp = await self.client.get(url)
                resp.raise_for_status()
                items.extend(self.parse(resp))
            except Exception as exc:              # noqa: BLE001
                self.errors.append(f"{url}: {type(exc).__name__}")
                if not self.partial_ok:
                    raise
        if self.errors and not items:
            raise RuntimeError("; ".join(self.errors))    # everything failed
        return items

    def parse(self, response: httpx.Response) -> list[dict]:
        raise NotImplementedError


def new_client() -> httpx.AsyncClient:
    """Shared client: one UA, sane timeout, follows the www -> apex redirects."""
    return httpx.AsyncClient(
        timeout=settings.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
    )
