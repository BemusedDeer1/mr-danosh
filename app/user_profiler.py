import asyncio
import json
import logging
from collections import defaultdict

from google import genai
from google.genai import types

from app.config import Settings
from app.database import DatabaseManager
from app.models import ProfileAnalysisOutput, TraitItem
from app.prompt_builder import PromptBuilder
from app.safety import SafetyGuard
from app.utils import normalize_persian

logger = logging.getLogger(__name__)


class UserProfiler:
    """یادگیری تدریجی و دسته‌ای پروفایل کاربران در پس‌زمینه با رعایت کامل حریم خصوصی."""

    def __init__(self, settings: Settings, db: DatabaseManager, batch_size: int = 15):
        self.settings = settings
        self.db = db
        self.batch_size = batch_size
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._pending_messages: dict[int, list[str]] = defaultdict(list)
        self._active_tasks: set[int] = set()

    def enqueue_message(self, user_id: int, clean_text: str) -> bool:
        """افزودن پیام تمیز به صف تحلیل کاربر؛ در صورت رسیدن به حد نصاب True برمی‌گرداند."""
        if not clean_text or len(clean_text.strip()) < 3:
            return False
        if SafetyGuard.contains_sensitive_data(clean_text):
            return False

        self._pending_messages[user_id].append(clean_text.strip()[:250])
        return len(self._pending_messages[user_id]) >= self.batch_size

    def schedule_analysis(self, user_id: int) -> None:
        """زمان‌بندی غیرمسدودکننده تحلیل پروفایل در پس‌زمینه."""
        if user_id in self._active_tasks:
            return
        self._active_tasks.add(user_id)
        asyncio.create_task(self._run_analysis_task(user_id))

    async def _run_analysis_task(self, user_id: int) -> None:
        try:
            # تأخیر کوتاه برای جلوگیری از تداخل با پاسخ‌گویی لحظه‌ای گروه
            await asyncio.sleep(3.0)

            profile = await self.db.get_user_profile(user_id)
            if not profile or profile.opted_out:
                self._pending_messages.pop(user_id, None)
                return

            batch = self._pending_messages[user_id][: self.batch_size]
            if len(batch) < 5:
                return

            sys_inst, prompt = PromptBuilder.build_profile_analysis_prompt(
                user_name=profile.full_name,
                current_summary=profile.persona_summary,
                messages=batch,
            )

            parsed = await self._call_structured_gemini(sys_inst, prompt)
            if not parsed:
                # شکست تحلیل نباید داده قبلی را خراب کند
                return

            # پاک کردن پیام‌های تحلیل‌شده از صف
            self._pending_messages[user_id] = self._pending_messages[user_id][len(batch) :]

            # ادغام امن و فیلتر دو مرحله‌ای خروجی مدل
            updated_profile = self._merge_analysis(profile, parsed)
            await self.db.save_user_profile(updated_profile)
            logger.info(f"Updated profile for user_id={user_id} ({profile.full_name})")

        except Exception as exc:
            logger.error(f"Profile analysis failed for user_id={user_id}: {exc}")
        finally:
            self._active_tasks.discard(user_id)

    async def _call_structured_gemini(
        self, system_instruction: str, prompt: str
    ) -> ProfileAnalysisOutput | None:
        """دریافت خروجی JSON ساختاریافته از Gemini با یک بار Retry در صورت نامعتبر بودن."""
        for attempt in range(2):
            try:
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.3,
                    max_output_tokens=350,
                    response_mime_type="application/json",
                    response_schema=ProfileAnalysisOutput,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.settings.AI_MODEL,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=20.0,
                )
                raw_json = (response.text or "").strip()
                data = json.loads(raw_json)
                return ProfileAnalysisOutput.model_validate(data)
            except Exception as exc:
                logger.warning(f"Structured profile parse attempt {attempt + 1} failed: {exc}")
                await asyncio.sleep(1.5)
        return None

    def _merge_analysis(self, profile, parsed: ProfileAnalysisOutput):
        """ادغام ویژگی‌های جدید با ویژگی‌های قبلی و ارتقای probable به confirmed."""
        existing_interests: dict[str, TraitItem] = {
            normalize_persian(item.value): item for item in profile.interests
        }

        for new_item in parsed.interests:
            if SafetyGuard.contains_sensitive_data(new_item.value):
                continue
            key = normalize_persian(new_item.value)
            if not key or len(key) > 40:
                continue
            if key in existing_interests:
                existing = existing_interests[key]
                existing.source_count += 1
                if existing.source_count >= 2:
                    existing.confidence = "confirmed"
            else:
                existing_interests[key] = TraitItem(
                    value=new_item.value.strip(), confidence="probable", source_count=1
                )

        # محدود کردن لیست علایق به ۱۲ مورد برتر
        merged_interests = sorted(
            existing_interests.values(),
            key=lambda x: (x.confidence == "confirmed", x.source_count),
            reverse=True,
        )[:12]

        # ادغام حقایق/روابط غیرحساس
        existing_facts: dict[str, TraitItem] = {
            normalize_persian(item.value): item for item in profile.relationships
        }
        for fact in parsed.known_facts:
            if SafetyGuard.contains_sensitive_data(fact.value):
                continue
            fkey = normalize_persian(fact.value)
            if not fkey or len(fkey) > 60:
                continue
            if fkey in existing_facts:
                existing_facts[fkey].source_count += 1
                if existing_facts[fkey].source_count >= 2:
                    existing_facts[fkey].confidence = "confirmed"
            else:
                existing_facts[fkey] = TraitItem(
                    value=fact.value.strip(), confidence="probable", source_count=1
                )

        profile.interests = merged_interests
        profile.relationships = list(existing_facts.values())[:8]

        if parsed.humor_style and not SafetyGuard.contains_sensitive_data(parsed.humor_style):
            profile.humor_style = parsed.humor_style.strip()[:40]

        if parsed.communication_style and not SafetyGuard.contains_sensitive_data(parsed.communication_style):
            profile.comm_style = parsed.communication_style.strip()[:40]

        if parsed.persona_summary and not SafetyGuard.contains_sensitive_data(parsed.persona_summary):
            profile.persona_summary = parsed.persona_summary.strip()[:250]

        return profile
