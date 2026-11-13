from typing import Literal, Optional
from pydantic import BaseModel, Field


class TraitItem(BaseModel):
    """یک ویژگی رفتاری یا علاقه کاربر با سطح اطمینان."""

    value: str
    confidence: Literal["confirmed", "probable"] = "probable"
    source_count: int = 1


class UserProfile(BaseModel):
    """مدل پروفایل بلندمدت کاربر در گروه."""

    user_id: int
    full_name: str
    username: Optional[str] = None
    msg_count: int = 0
    first_seen: str
    last_seen: str
    interests: list[TraitItem] = Field(default_factory=list)
    humor_style: str = "نامشخص"
    comm_style: str = "معمولی"
    relationships: list[TraitItem] = Field(default_factory=list)
    inside_jokes: list[str] = Field(default_factory=list)
    persona_summary: str = "هنوز شناخت زیادی از این دانش‌آموز ثبت نشده است."
    discipline_score: int = 100
    no_personal_jokes: bool = False
    opted_out: bool = False
    updated_at: str


class ProfileAnalysisOutput(BaseModel):
    """ساختار خروجی استاندارد Gemini برای تحلیل دسته‌ای پروفایل."""

    interests: list[TraitItem] = Field(default_factory=list)
    humor_style: str = "نامشخص"
    communication_style: str = "معمولی"
    known_facts: list[TraitItem] = Field(default_factory=list)
    persona_summary: str = ""


class EpisodeRecord(BaseModel):
    """مدل یک خاطره یا رویداد مهم در حافظه اپیزودیک."""

    id: Optional[int] = None
    chat_id: int
    summary: str
    event_date: str
    user_ids: list[int] = Field(default_factory=list)
    tags: str = ""
    importance: int = Field(default=3, ge=1, le=5)
    last_recalled_at: Optional[str] = None
    recall_count: int = 0
