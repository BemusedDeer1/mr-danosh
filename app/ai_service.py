import asyncio
import logging
import random
import re
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
    """موتور ترکیبی Gemini + Groq با تنظیمات بهینه برای دستور زبان فارسی طبیعی."""

    CORE_FALLBACKS = [
        "الان حواسم پرت پرونده‌های دفتر بود، یه بار دیگه بگو ببینم چی میگی؟",
        "صبر کن ببینم، الان خط‌کشم افتاد زمین! دوباره بگو قضیه چیه؟",
        "آقا یه لحظه آروم بگیرید، صدای همتون قاطی شد نفهمیدم چی گفتی!",
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

        self._groq_models_cache: list[str] = []
        self._groq_models_fetched_at: float = 0.0
        self._groq_cooldown_until: dict[str, float] = {}

        self._cache: dict[str, tuple[str, float]] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        self._running = True
        self.http_client = httpx.AsyncClient(timeout=22.0)
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(2)]
        groq_status = "enabled (Persian-optimized rotation)" if self.settings.GROQ_API_KEY else "disabled"
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

    @staticmethod
    def _strip_reasoning_tags(raw_text: str) -> str:
        """حذف بخش‌های فکر کردن مخفی (<think> و <thought>) و استخراج پاسخ نهایی."""
        text = raw_text.strip()
        if "</think>" in text:
            text = text.split("</think>")[-1].strip()
        if "</thought>" in text:
            text = text.split("</thought>")[-1].strip()
        text = re.sub(r"<(think|thought)>.*?</\1>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"</?(think|thought)>", "", text).strip()
        return text

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
            result = await asyncio.wait_for(task.future, timeout=45.0)
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
        gemini_err: Optional[Exception] = None

        if time.time() >= self.gemini_open_until:
            try:
                text = await self._call_gemini_chain(system_instruction, prompt)
                self.gemini_failures = 0
                return text
            except Exception as exc:
                gemini_err = exc
                self.gemini_failures += 1
                threshold = 2 if "429" in str(exc) else 4
                if self.gemini_failures >= threshold:
                    self.gemini_open_until = time.time() + 180
                    logger.warning("Gemini circuit breaker OPEN for 180s; routing directly to Groq.")
        else:
            logger.info("Gemini circuit is open; skipping straight to Groq.")

        if self.settings.GROQ_API_KEY:
            try:
                text, used_model = await self._call_groq_rotated(system_instruction, prompt)
                logger.info(f"Successfully generated reply via Groq ({used_model}).")
                return text
            except Exception as groq_exc:
                logger.error(f"All Groq models failed: {groq_exc}")
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
                    temperature=0.6,
                    max_output_tokens=700,
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
                text = self._strip_reasoning_tags(response.text or "")
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
                    await asyncio.sleep(0.4)

        assert last_exc is not None
        raise last_exc

    async def _get_ranked_groq_models(self) -> list[str]:
        """انتخاب مدل‌های قوی و خوش‌نگارش در زبان فارسی و حذف مدل‌های ضعیف."""
        now = time.time()
        if self._groq_models_cache and (now - self._groq_models_fetched_at < 600):
            return self._groq_models_cache

        assert self.http_client is not None
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": f"Bearer {self.settings.GROQ_API_KEY}"}
        try:
            resp = await self.http_client.get(url, headers=headers)
            if resp.status_code != 200:
                return [self.settings.GROQ_MODEL]
        except Exception:
            return [self.settings.GROQ_MODEL]

        models_data = resp.json().get("data", [])
        # حذف مدل‌های صوتی، تصویری و مدل‌های کوچک 8B که فارسی را خراب می‌کنند
        excluded = ("whisper", "guard", "tts", "embed", "vision", "orpheus", "safeguard", "8b")
        raw_ids = [
            m["id"]
            for m in models_data
            if m.get("id") and not any(bad in m["id"].lower() for bad in excluded)
        ]

        # اولویت با مدل‌هایی که فارسی محاوره‌ای را دقیق و بدون غلط املایی می‌نویسند
        priority_order = ("qwen", "gpt-oss-120b", "70b", "llama-4", "gpt-oss-20b", "gemma")
        ranked: list[str] = []
        for keyword in priority_order:
            for mid in raw_ids:
                if keyword in mid.lower() and mid not in ranked:
                    ranked.append(mid)
        for mid in raw_ids:
            if mid not in ranked:
                ranked.append(mid)

        if ranked:
            self._groq_models_cache = ranked
            self._groq_models_fetched_at = now
            logger.info(f"Discovered {len(ranked)} Persian-capable Groq models: {ranked}")
        return ranked or [self.settings.GROQ_MODEL]

    async def _call_groq_rotated(self, system_instruction: str, prompt: str) -> tuple[str, str]:
        """فراخوانی Groq بدون frequency_penalty تا پیشوند و پسوند فعل‌های فارسی خراب نشوند."""
        assert self.http_client is not None
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        all_models = await self._get_ranked_groq_models()
        now = time.time()

        available = [m for m in all_models if now >= self._groq_cooldown_until.get(m, 0.0)]
        if not available:
            available = all_models

        last_exc: Optional[Exception] = None
        for model_id in available[:4]:
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.55,
                "top_p": 0.9,
                "frequency_penalty": 0.0,
                "presence_penalty": 0.0,
                "max_tokens": 600,
            }
            try:
                resp = await self.http_client.post(url, headers=headers, json=payload)
            except Exception as net_exc:
                last_exc = net_exc
                continue

            if resp.status_code == 429:
                self._groq_cooldown_until[model_id] = time.time() + 45
                last_exc = RuntimeError(f"Groq 429 TPM limit on {model_id}")
                logger.warning(f"Groq model '{model_id}' hit 429; rotating to next model...")
                continue

            if resp.status_code == 404:
                self._groq_cooldown_until[model_id] = time.time() + 3600
                last_exc = RuntimeError(f"Groq 404 on {model_id}")
                continue

            if resp.status_code != 200:
                last_exc = RuntimeError(f"Groq HTTP {resp.status_code}: {resp.text[:120]}")
                continue

            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"] or ""
            text = self._strip_reasoning_tags(raw_content)
            if not text:
                last_exc = ValueError(f"Empty response from Groq '{model_id}'.")
                continue

            est_tokens = (len(system_instruction) + len(prompt) + len(text)) // 3
            await self.db.log_api_usage(self._get_today_str(), est_tokens)
            return text, model_id

        if last_exc:
            raise last_exc
        raise RuntimeError("No Groq models available.")
