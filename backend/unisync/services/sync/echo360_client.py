import httpx
import logging
import asyncio
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# P1-9: parallelize syllabus fetch — semaphore caps concurrent Echo360 API hits
_SYLLABUS_FETCH_SEM = asyncio.Semaphore(5)


class Echo360Client:
    def __init__(self, base_url: str, cookies: dict[str, str]):
        self.base_url = base_url.rstrip("/")
        # We spoof the Referer to bypass Echo360's cross-origin protections
        # natively, replacing the need for the userscript Dual-Mode hack.
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            headers={"Referer": self.base_url + "/"},
            timeout=httpx.Timeout(30.0, read=600.0)
        )

    async def close(self):
        await self.client.aclose()

    async def get_user_sections(self) -> list[dict[str, Any]]:
        """Fetch Echo360 course sections."""
        url = "/user/sections"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            return data.get("data", [])
        except Exception as e:
            logger.error(f"Failed to fetch Echo360 sections: {e}")
            return []

    async def get_syllabus(self, section_id: str) -> list[dict[str, Any]]:
        """Fetch the full syllabus for a single section."""
        url = f"/section/{section_id}/syllabus"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            return data.get("data", [])
        except Exception as e:
            logger.error(f"Failed to fetch Echo360 syllabus for {section_id}: {e}")
            return []

    # P1-9: parallelize syllabus fetch — gather all sections under a semaphore
    async def get_all_syllabi(self, section_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch syllabi for every *section_id* concurrently (max 5 in-flight).

        Each individual failure is swallowed so one bad section does not
        abort the rest — the failing section maps to an empty list.
        """
        async def _guarded(sid: str) -> tuple[str, list[dict[str, Any]]]:
            async with _SYLLABUS_FETCH_SEM:
                try:
                    syllabus = await self.get_syllabus(sid)
                    return (sid, syllabus)
                except Exception as exc:
                    logger.error(f"P1-9: parallel syllabus fetch failed for {sid}: {exc}")
                    return (sid, [])

        results = await asyncio.gather(*[_guarded(sid) for sid in section_ids])
        return dict(results)

    async def download_media(self, media_url: str, dest_path: Path):
        """Streams a lecture video directly to disk."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self.client.stream("GET", media_url) as response:
                response.raise_for_status()
                with dest_path.open("wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
            logger.info(f"Downloaded Echo360 media to {dest_path}")
            try:
                from unisync.core.download_registry import register_download
                register_download(
                    platform="echo360",
                    item_id=dest_path.stem,
                    title=dest_path.name,
                    direct_link=media_url,
                    local_filepath=dest_path,
                    size_bytes=dest_path.stat().st_size if dest_path.exists() else 0,
                    content_type="video/mp4",
                )
            except Exception as reg_err:
                logger.debug("Download registry hook error: %s", reg_err)
        except Exception as e:
            logger.error(f"Failed to download {media_url}: {e}")
