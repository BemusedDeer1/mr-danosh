import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from app.config import Settings
from app.database import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass(order=True)
class AIRequestTask:
    priority: int
    created_at: float = field(compare=True)
    system_instruction: str = field(compare=False)
    prompt: str = field(compare=False)
    use_cache: bool = field(default=False, compare=False)
    cache_key: Optional[str] = field(default=None, compare=False)
    future: asyncio.Future = field(default_factory=asyncio.Future, compare=False)


class AIService:
    """مدیریت صف اولویت‌دار Gemini، Circuit Breaker، Cache و Fallback."""

    CORE_FALLBACKS = [
        "ببین الان خط‌کشم افتاد زمین، حواسم پرت شد! دوباره بگو ببینم قضیه چیه؟",
        "نه ببین، این حرفی که زدی خودش جای بررسی انضباطی داره، فعلاً بگذریم تا بعد!",
        "آقا شلوغ نکنید! یه لحظه سکوت رو رعایت کنید تا پرونده‌تون رو بررسی کنم.",
        "اینا همش بازیه عزیزم، می‌خوان با همین بحث‌ها حواستون رو از درس و نظم پرت کنن!",
    ]

    def __init__(self, settings: Settings, db: DatabaseManager):
        self.settings = settings
        self.db = db
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.queue: asyncio.PriorityQueue[AIRequestTask] = asyncio.PriorityQueue(maxsize=100)
        self.semaphore = asyncio.Semaphore(2)

        self.consecutive_failures = 0
        self.circuit_open_until: float = 0.0

        self._cache: dict[str, tuple[str, float]] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(2)]
        logger.info(f"AIService started with model '{self.settings.AI_MODEL}'.")

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("AIService stopped.")

    def _get_today_str(self) -> str:
        return datetime.now(ZoneInfo(self.settings.TIMEZONE)).strftime("%Y-%m-%d")

    def _get_cached(self, key: str) -> Optional[str]:
        item = self._cache.get(key)
        if not item:
            return None
        val, expires_at = item
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return val

    def _set_cached(self, key: str, val: str, ttl_sec: int = 3600) -> None:
        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[key] = (val, time.time() + ttl_sec)

    def _build_safety_settings(self) -> Optional[list[types.SafetySetting]]:
        """تنظیم سطح فیلتر ایمنی جمنای بر اساس متغیر AI_ENABLE_SAFETY_FILTERS."""
        if self.settings.AI_ENABLE_SAFETY_FILTERS:
            return None
        categories = [
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        ]
        return [
            types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for cat in categories
        ]

    async def generate_text(
        self,
        system_instruction: str,
        prompt: str,
        priority: int = 1,
        use_cache: bool = False,
        cache_key: Optional[str] = None,
        fallback_pool: Optional[list[str]] = None,
    ) -> str:
        """ارسال درخواست تولید متن به صف اولویت‌دار با بازگشت خودکار به Fallback در صورت خطا."""
        fallbacks = fallback_pool or self.CORE_FALLBACKS

        if use_cache and cache_key:
            cached = self._get_cached(cache_key)
            if cached:
                return cached

        if time.time() < self.circuit_open_until:
            logger.warning("Circuit breaker is OPEN; returning fallback response.")
            return random.choice(fallbacks)

        today = self._get_today_str()
        daily_count = await self.db.get_daily_api_count(today)
        if daily_count >= self.settings.DAILY_API_CALL_LIMIT:
            logger.warning("Daily API call limit reached; returning fallback response.")
            return random.choice(fallbacks)

        loop = asyncio.get_running_loop()
        task = AIRequestTask(
            priority=priority,
            created_at=time.time(),
            system_instruction=system_instruction,
            prompt=prompt,
            use_cache=use_cache,
            cache_key=cache_key,
            future=loop.create_future(),
        )

        try:
            self.queue.put_nowait(task)
            result = await asyncio.wait_for(task.future, timeout=45.0)
            return result
        except Exception as exc:
            logger.error(f"AI generation failed or timed out: {exc}")
            return random.choice(fallbacks)

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                async with self.semaphore:
                    response_text = await self._call_gemini_with_retry(
                        task.system_instruction, task.prompt
                    )
                    if task.use_cache and task.cache_key and response_text:
                        self._set_cached(task.cache_key, response_text)
                    if not task.future.done():
                        task.future.set_result(response_text)
            except Exception as exc:
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self.queue.task_done()

    async def _call_gemini_with_retry(self, system_instruction: str, prompt: str) -> str:
        """فراخوانی SDK رسمی google-genai با Exponential Backoff و مدیریت 429/503."""
        max_retries = 2
        base_delay = 2.0
        safety_settings = self._build_safety_settings()

        for attempt in range(max_retries + 1):
            try:
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=self.settings.AI_TEMPERATURE,
                    max_output_tokens=self.settings.AI_MAX_OUTPUT_TOKENS,
                    safety_settings=safety_settings,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.settings.AI_MODEL,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=20.0,
                )
                text = (response.text or "").strip()
                if not text:
                    raise ValueError("Empty response received from Gemini.")

                self.consecutive_failures = 0
                est_tokens = (len(system_instruction) + len(prompt) + len(text)) // 3
                await self.db.log_api_usage(self._get_today_str(), est_tokens)
                return text

            except Exception as exc:
                err_str = str(exc)
                self.consecutive_failures += 1
                if self.consecutive_failures >= 5:
                    self.circuit_open_until = time.time() + 180
                    logger.error("Circuit breaker tripped for 180s due to repeated API failures.")

                if attempt < max_retries:
                    delay = base_delay * (2**attempt) + random.uniform(0.2, 1.0)
                    if "429" in err_str or "503" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        delay += 3.0
                    logger.warning(f"Gemini API error (attempt {attempt + 1}), retrying in {delay:.1f}s: {exc}")
                    await asyncio.sleep(delay)
                else:
                    raise
        raise RuntimeError("Unreachable retry exit")
