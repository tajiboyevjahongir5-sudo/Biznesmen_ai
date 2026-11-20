"""Bepul litsenziyali fotolar: Pexels API (https://www.pexels.com/api/).

Pexels litsenziyasi fotolardan bepul, jumladan tijoriy maqsadda foydalanishga ruxsat beradi.
API qoidasiga ko'ra manba (Pexels) va fotograf ko'rsatiladi — post ostiga `credit` qo'shiladi.

Xavfsizlik: kalit faqat `Authorization` sarlavhasida yuboriladi (URL ga va logga tushmaydi);
rasm faqat Pexels CDN dan (https://images.pexels.com) yuklanadi, boshqa manzilga so'rov yo'q.
"""

from __future__ import annotations

import logging
import random
import re
from collections import deque
from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.gemini_native import IMAGE_TYPES, MAX_IMAGE_BYTES, GeneratedImage

logger = logging.getLogger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
IMAGE_HOST = "images.pexels.com"
PAGE_HOSTS = ("www.pexels.com", "pexels.com")
RECENT_LIMIT = 60  # yaqinda ishlatilgan fotolar takrorlanmasin
PICK_FROM_TOP = 5  # eng mos 5 tadan tasodifiy — kanal bir xil ko'rinmasin


class StockPhotoError(Exception):
    """Foydalanuvchiga (adminga) ko'rsatsa bo'ladigan sabab bilan."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class StockPhoto:
    id: int
    image_url: str
    page_url: str
    photographer: str
    photographer_url: str


def clean_query(text: str) -> str:
    """Qidiruv so'zini xavfsiz qiladi: lotin harflari, raqam, bo'sh joy va chiziqcha, 80 belgigacha."""
    words = re.sub(r"[^A-Za-z0-9 \-]", " ", text or "").split()
    return " ".join(words)[:80].strip()


def _https_on(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "") in hosts


def photo_credit(photo: StockPhoto) -> str:
    """Post uchun HTML: muallif va manba. Havolalar faqat pexels.com ga."""
    name = escape(photo.photographer[:60] or "Pexels", quote=False)
    if _https_on(photo.photographer_url, PAGE_HOSTS):
        name = f'<a href="{escape(photo.photographer_url)}">{name}</a>'
    source = "Pexels"
    if _https_on(photo.page_url, PAGE_HOSTS):
        source = f'<a href="{escape(photo.page_url)}">Pexels</a>'
    return f"📷 {name} · {source}"


class PexelsClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = http
        self._recent: deque[int] = deque(maxlen=RECENT_LIMIT)

    @property
    def enabled(self) -> bool:
        return bool(self._s.pexels_api_key.get_secret_value())

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def search(self, query: str, per_page: int = 15) -> list[StockPhoto]:
        key = self._s.pexels_api_key.get_secret_value()
        if not key:
            raise StockPhotoError("PEXELS_API_KEY kiritilmagan")
        params = {"query": query, "per_page": per_page, "orientation": "landscape"}
        try:
            resp = await self._client().get(PEXELS_SEARCH_URL, params=params, headers={"Authorization": key})
        except httpx.HTTPError as exc:
            raise StockPhotoError(f"Pexels'ga ulanib bo'lmadi ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise StockPhotoError("Pexels kaliti noto'g'ri yoki bloklangan", resp.status_code)
        if resp.status_code == 429:
            raise StockPhotoError("Pexels limiti tugadi (soatiga 200 so'rov) — keyinroq", 429)
        if resp.status_code != 200:
            raise StockPhotoError(f"Pexels xatosi: HTTP {resp.status_code}", resp.status_code)
        try:
            items = resp.json().get("photos") or []
        except (ValueError, AttributeError) as exc:
            raise StockPhotoError("Pexels javobi buzilgan") from exc
        photos: list[StockPhoto] = []
        for item in items:
            src = item.get("src") or {}
            url = str(src.get("landscape") or src.get("large") or "")
            if not _https_on(url, (IMAGE_HOST,)) or not isinstance(item.get("id"), int):
                continue
            photos.append(
                StockPhoto(
                    id=item["id"],
                    image_url=url,
                    page_url=str(item.get("url") or ""),
                    photographer=str(item.get("photographer") or ""),
                    photographer_url=str(item.get("photographer_url") or ""),
                )
            )
        return photos

    async def download(self, photo: StockPhoto) -> tuple[bytes, str]:
        if not _https_on(photo.image_url, (IMAGE_HOST,)):
            raise StockPhotoError("rasm manzili Pexels emas")
        try:
            resp = await self._client().get(photo.image_url)  # yo'naltirishlar kuzatilmaydi
        except httpx.HTTPError as exc:
            raise StockPhotoError(f"rasmni yuklab bo'lmadi ({type(exc).__name__})") from exc
        mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code != 200 or mime not in IMAGE_TYPES:
            raise StockPhotoError(f"rasmni yuklab bo'lmadi (HTTP {resp.status_code})", resp.status_code)
        if not resp.content or len(resp.content) > MAX_IMAGE_BYTES:
            raise StockPhotoError("rasm hajmi noto'g'ri")
        return resp.content, mime

    async def find(self, query: str) -> GeneratedImage:
        """Qidiruv so'zi bo'yicha mos fotoni topadi va yuklaydi (yaqinda ishlatilganlarsiz)."""
        query = clean_query(query)
        if not query:
            raise StockPhotoError("qidiruv so'zi bo'sh")
        photos = await self.search(query)
        fresh = [p for p in photos if p.id not in self._recent] or photos
        if not fresh:
            raise StockPhotoError(f"«{query}» bo'yicha foto topilmadi")
        photo = random.choice(fresh[:PICK_FROM_TOP])
        data, mime = await self.download(photo)
        self._recent.append(photo.id)
        logger.info("stock_photo_found id=%d bytes=%d", photo.id, len(data))
        return GeneratedImage(data, mime, "pexels", photo_credit(photo))
