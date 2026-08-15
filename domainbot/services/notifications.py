from __future__ import annotations

import logging

import httpx

from domainbot.config import Settings


logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send(self, message: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": message}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("telegram notification failed: %s", exc)

    async def watching(self, domain: str) -> None:
        await self.send(f"WATCHING {domain}")

    async def available(self, domain: str) -> None:
        await self.send(f"AVAILABLE {domain}")

    async def registering(self, domain: str) -> None:
        await self.send(f"REGISTERING {domain}")

    async def success(self, domain: str, *, price: str | None = None, order_id: str | None = None) -> None:
        lines = [f"SUCCESS {domain}"]
        if price:
            lines.append(f"Price: {price}")
        if order_id:
            lines.append(f"Order: {order_id}")
        await self.send("\n".join(lines))

    async def failed(self, domain: str, reason: str) -> None:
        await self.send(f"FAILED {domain}\nReason: {reason}")

    async def timeout(self, domain: str, duration_seconds: int) -> None:
        await self.send(f"TIMEOUT {domain}\nNo registration during {duration_seconds // 60} minute window")

    async def price_exceeded(self, domain: str, *, actual: str, limit: str) -> None:
        await self.send(f"PRICE EXCEEDED {domain}\nActual: {actual}\nLimit: {limit}")

