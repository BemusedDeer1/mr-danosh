import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
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
    """موتور ترکیبی Gemini + Groq با کشف خودکار مدل فعال، Circuit Breaker مجزا و Fallback."""

    CORE_FALLBACKS = [
        "ببین الان خط‌کشم افتاد زمین، حواسم پرت شد! دوباره بگو ببینم قضیه چیه؟",
        "نه ببین، این حرفی که زدی خودش جای بررسی انضباطی داره، فعلاً بگذریم تا بعد!",
        "آقا شلوغ نکنید! یه لحظه سکوت رو رعایت کنید تا پرونده‌تون رو بررسی کنم.",
        "اینا همش بازیه عزیزم، می‌خوان با همین بحث‌ها حواستون رو از درس و نظم پرت کنن!",
    ]

    GEMINI_BACKUPS = [
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]

    def __init__(self, settings: Settings, db: DatabaseManager):
        self.settings = settings
        self.db = db
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.http_client: Optional[httpx.AsyncClient] = None

        self.queue: asyncio.PriorityQueue[AIRequestTask] = asyncio.PriorityQueue(maxsize=100)
        self.semaphore = asyncio.Semaphore(2)

        self.gemini_failures = 0
        self.gemini_open_until: float = 0.0
        self._active_groq_model: str = settings.GROQ_MODEL

        self._cache: dict[str, tuple[str, float]] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        self._running = True
        self.http_client = httpx.AsyncClient(timeout=20.0)
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(2)]
        groq_status = f"enabled ({self._active_groq_model})" if self.settings.GROQ_API_KEY else "disabled"
        logger.info(
            f"AIService started | Primary Gemini: '{self.settings.AI_MODEL}' | Backup Groq: {groq_status}"
        )

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        if self.http_client:
            await self.http_client.aclose()
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

    def _get_gemini_chain(self) -> list[str]:
        chain = [self.settings.AI_MODEL]
        for m in self.GEMINI_BACKUPS:
            if m not in chain:
                chain.append(m)
        return chain[:2]

    async def generate_text(
        self,
        system_instruction: str,
        prompt: str,
        priority: int = 1,
        use_cache: bool = False,
        cache_key: Optional[str] = None,
        fallback_pool: Optional[list[str]] = None,
    ) -> str:
        fallbacks = fallback_pool or self.CORE_FALLBACKS

        if use_cache and cache_key:
            cached = self._get_cached(cache_key)
            if cached:
                return cached

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
            result = await asyncio.wait_for(task.future, timeout=40.0)
            return result
        except Exception as exc:
            logger.error(f"All AI providers failed or timed out: {exc}")
            return random.choice(fallbacks)

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                async with self.semaphore:
                    response_text = await self._generate_with_failover(
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

    async def _generate_with_failover(self, system_instruction: str, prompt: str) -> str:
        """تلاش با Gemini و در صورت شلوغی یا خطا، سوئیچ فوری روی Groq."""
        gemini_err: Optional[Exception] = None

        if time.time() >= self.gemini_open_until:
            try:
                text = await self._call_gemini_chain(system_instruction, prompt)
                self.gemini_failures = 0
                return text
            except Exception as exc:
                gemini_err = exc
                self.gemini_failures += 1
                if self.gemini_failures >= 4:
                    self.gemini_open_until = time.time() + 180
                    logger.warning("Gemini circuit breaker OPEN for 180s; routing directly to Groq.")
        else:
            logger.info("Gemini circuit is open; skipping straight to Groq.")

        if self.settings.GROQ_API_KEY:
            try:
                text = await self._call_groq(system_instruction, prompt)
                logger.info(f"Successfully generated reply via Groq ({self._active_groq_model}).")
                return text
            except Exception as groq_exc:
                logger.error(f"Groq backup also failed: {groq_exc}")
                raise groq_exc

        if gemini_err:
            raise gemini_err
        raise RuntimeError("No AI provider succeeded.")

    async def _call_gemini_chain(self, system_instruction: str, prompt: str) -> str:
        models_to_try = self._get_gemini_chain()
        safety_settings = self._build_safety_settings()
        last_exc: Optional[Exception] = None

        for idx, model_name in enumerate(models_to_try):
            try:
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=self.settings.AI_TEMPERATURE,
                    max_output_tokens=self.settings.AI_MAX_OUTPUT_TOKENS,
                    safety_settings=safety_settings,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=14.0,
                )
                text = (response.text or "").strip()
                if not text:
                    raise ValueError(f"Empty response from Gemini '{model_name}'.")

                est_tokens = (len(system_instruction) + len(prompt) + len(text)) // 3
                await self.db.log_api_usage(self._get_today_str(), est_tokens)
                if idx > 0:
                    logger.info(f"Recovered using backup Gemini model '{model_name}'.")
                return text
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Gemini '{model_name}' failed: {str(exc)[:90]}")
                if idx < len(models_to_try) - 1:
                    await asyncio.sleep(0.8)

        assert last_exc is not None
        raise last_exc

    async def _discover_best_groq_model(self) -> Optional[str]:
        """دریافت خودکار لیست مدل‌های فعال اکانت Groq در صورت منقضی شدن مدل پیش‌فرض."""
        assert self.http_client is not None
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": f"Bearer {self.settings.GROQ_API_KEY}"}
        resp = await self.http_client.get(url, headers=headers)
        if resp.status_code != 200:
            return None

        models_data = resp.json().get("data", [])
        excluded_keywords = ("whisper", "guard", "tts", "embed", "vision", "orpheus")
        candidates = [
            m["id"]
            for m in models_data
            if m.get("id") and not any(bad in m["id"].lower() for bad in excluded_keywords)
        ]
        if not candidates:
            return None

        # اولویت‌بندی مدل‌های بزرگ و قوی برای زبان فارسی
        for pref in ("70b", "qwen", "llama-4", "llama-3", "gemma", "8b"):
            for cid in candidates:
                if pref in cid.lower():
                    return cid
        return candidates[0]

    async def _call_groq(self, system_instruction: str, prompt: str) -> str:
        """فراخوانی Groq API با کشف خودکار مدل در صورت خطای 404."""
        assert self.http_client is not None
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        for attempt in range(2):
            payload = {
                "model": self._active_groq_model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "temperature": self.settings.AI_TEMPERATURE,
                "max_tokens": self.settings.AI_MAX_OUTPUT_TOKENS,
            }
            resp = await self.http_client.post(url, headers=headers, json=payload)

            if resp.status_code == 404 and attempt == 0:
                discovered = await self._discover_best_groq_model()
                if discovered and discovered != self._active_groq_model:
                    logger.info(f"Groq model '{self._active_groq_model}' unavailable; auto-switched to '{discovered}'.")
                    self._active_groq_model = discovered
                    continue

            if resp.status_code != 200:
                raise RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:150]}")

            data = resp.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            if not text:
                raise ValueError("Empty response from Groq.")

            est_tokens = (len(system_instruction) + len(prompt) + len(text)) // 3
            await self.db.log_api_usage(self._get_today_str(), est_tokens)
            return text

        raise RuntimeError("Groq failed after model discovery retry.")
