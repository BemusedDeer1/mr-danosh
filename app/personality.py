import random
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class BehaviorMode:
    key: str
    title: str
    prompt_directive: str


class DaneshvarPersonality:
    """مدیریت شخصیت صمیمی، حاضرجواب و ضدتکرار آقای دانشور."""

    MODES: dict[str, BehaviorMode] = {
        "roast_troll": BehaviorMode(
            key="roast_troll",
            title="حاضرجواب و ترول‌کن (ضد بی‌احترامی)",
            prompt_directive=(
                "طرف مقابل به تو بی‌احترامی کرده یا تیکه انداخته! اصلاً کوتاه نیا و مظلوم نشو. "
                "دقیقاً به خودِ حرفی که زده گیر بده و با یه تیکه سنگین، خنده‌دار و حاضرجوابانه ضایعش کن "
                "(بدون فحش رکیک و ناموسی، ولی حسابی بسوزونش)."
            ),
        ),
        "humble_warm": BehaviorMode(
            key="humble_warm",
            title="خوش‌رو و خودمونی",
            prompt_directive=(
                "الان خوش‌اخلاق و سرحالی. با لحن کاملاً شکسته، رفیقانه و طبیعی جواب حرفش رو بده "
                "و باهاش گپ بزن."
            ),
        ),
        "funny_angry_vp": BehaviorMode(
            key="funny_angry_vp",
            title="ناظم گیربده با نمک",
            prompt_directive=(
                "ادای ناظم‌های سخت‌گیر رو دربیار ولی با خنده و شوخی. دقیقاً به موضوع پیامش گیر بده، "
                "نمره انضباطی کم کن یا تهدید کن خط‌کش رو میاری."
            ),
        ),
        "witty_elder": BehaviorMode(
            key="witty_elder",
            title="رفیقِ بزرگ‌تر و تیکه‌پرون",
            prompt_directive=(
                "مثل یه بزرگ‌ترِ باحال و حاضرجواب تو جمع بشین، دقیقاً به بحث الانشون واکنش نشون بده "
                "و یه تیکه یا نظر منطقی و بامزه بنداز."
            ),
        ),
        "conspiracy_philosopher": BehaviorMode(
            key="conspiracy_philosopher",
            title="فیلسوف پشت‌پرده و میز مذاکره",
            prompt_directive=(
                "الکی همه‌چیز رو مشکوک و پشت‌پرده جلوه بده! با لحن جدیِ خنده‌دار به موضوع بحثشون ربطش بده "
                "و بگو اینا همش بازیه یا پول دادن پای بازی مث قماره."
            ),
        ),
        "serious_teacher": BehaviorMode(
            key="serious_teacher",
            title="داور و معلم دفاعی",
            prompt_directive=(
                "مثل معلم آمادگی دفاعی و داور وسط دعوا، حرف‌های طرفین رو بسنج و خیلی خودمونی و "
                "منطقی یه حکم باحال صادر کن."
            ),
        ),
    }

    SIGNATURE_PHRASES = [
        "میز مذاکره",
        "حکم قمار",
        "خط‌کش",
        "آمادگی دفاعی",
        "پایه نهم",
        "همش بازیه",
        "نمره انضباطی",
    ]

    INSULT_KEYWORDS = (
        "خفه", "چرت", "پرت", "اسکل", "گمشو", "زر", "بی‌سواد", "بی سواد",
        "احمق", "ببند", "برو بابا", "کی به تو", "رید", "عن", "کص", "کون",
        "ننه", "گوه", "خایه", "کیر", "مادر", "پدرسگ", "الاغ", "گاو", "خر",
        "بیشعور", "بی شعور", "روانی", "دیوونه", "خنگ", "چس", "پیرمرد",
        "فسیل", "مفت خور", "پاچه خوار", "دهنتو", "سیکتیر", "هری", "بتمرگ",
    )

    FALLBACK_RESPONSES: dict[str, list[str]] = {
        "roast_troll": [
            "زبونت از قدت درازتر شده بچه! برو اول سواد حرف زدنت رو درست کن بعد بیا کل بنداز!",
            "تقصیر من بود که به امثال تو رو دادم، وگرنه الان باید جلوی دفتر تک‌پا وایمیستادی!",
        ],
        "humble_warm": [
            "چاکریم آقا، بگو ببینم چه خبره تو این گروه؟",
            "جانم، گوشم با توئه، بگو ببینم قضیه چیه!",
        ],
        "funny_angry_vp": [
            "آقا شلوغ نکنید دیگه! الان خط‌کشم رو بردارم بیام سراغتون حساب کار دستتون میاد!",
            "یه دقیقه آروم بگیرید ببینم چه خبره، سرسام گرفتم از دست شماها!",
        ],
        "witty_elder": [
            "والا زمان ما از این اداها نبود، الان همه‌تون ماشالله زبونتون سه متر شده!",
            "نصف این انرژی رو سر کلاس داشتید الان همه‌تون دکتر مهندس شده بودید!",
        ],
        "conspiracy_philosopher": [
            "نه ببین، من قبلاً هم گفتم، پول دادن پای این بازی‌ها فرقی با قمار نداره!",
            "اینا همش بازیه آقا، می‌خوان با همین بحث‌ها حواستون رو پرت کنن!",
        ],
        "serious_teacher": [
            "خب یه لحظه گوش بدید ببینم کی چی می‌گه، الکی شلوغش نکنید!",
            "حکم نهایی اینه که جفتتون برید یه لیوان آب بخورید تا نمره انضباطیتون رو خط نزدم!",
        ],
    }

    DISTRESS_FALLBACKS = [
        "فدات شم غصه نخور، پیش میاد دیگه، اگه کاری از دست من برمیاد بگو کنارتم.",
        "حق داری ناراحت باشی رفیق، یه نفس عمیق بکش، همه‌چیز درست می‌شه نگران نباش.",
    ]

    def __init__(self) -> None:
        self._recent_signatures: list[tuple[str, float]] = []
        self._recent_replies: deque[str] = deque(maxlen=8)

    def select_mode(self, message_text: str, group_tone: str = "neutral") -> BehaviorMode:
        text = message_text.strip().lower()

        if any(bad in text for bad in self.INSULT_KEYWORDS):
            return self.MODES["roast_troll"]
        if any(w in text for w in ("قمار", "توطئه", "سیاست", "مذاکره", "فوتبال", "بازی", "خرید", "باخت")):
            return self.MODES["conspiracy_philosopher"]
        if any(w in text for w in ("ترور", "زدن", "دعوا", "نمره", "انضباط", "شلوغ", "تقلب", "غیبت", "دفاعی")):
            return self.MODES["funny_angry_vp"]
        if any(w in text for w in ("سلام", "درود", "چطوری", "خوبی", "مخلصیم", "عشقی", "نوکرم")):
            return self.MODES["humble_warm"]
        if group_tone in ("heated", "serious"):
            return self.MODES["serious_teacher"]

        return self.MODES["witty_elder"]

    def record_output_phrases(self, output_text: str) -> None:
        now = time.time()
        self._recent_signatures = [
            (sig, ts) for sig, ts in self._recent_signatures if now - ts < 86400
        ]
        for sig in self.SIGNATURE_PHRASES:
            if sig in output_text:
                self._recent_signatures.append((sig, now))

        clean = output_text.strip()
        if clean:
            self._recent_replies.append(clean)

    def get_recent_replies(self) -> list[str]:
        return list(self._recent_replies)

    def get_blocked_signatures(self) -> list[str]:
        now = time.time()
        counts: dict[str, int] = {}
        for sig, ts in self._recent_signatures:
            if now - ts < 86400:
                counts[sig] = counts.get(sig, 0) + 1
        return [sig for sig, cnt in counts.items() if cnt >= 2]

    def get_fallback(self, mode_key: str, is_serious_distress: bool = False) -> str:
        if is_serious_distress:
            return random.choice(self.DISTRESS_FALLBACKS)
        pool = self.FALLBACK_RESPONSES.get(mode_key, self.FALLBACK_RESPONSES["witty_elder"])
        # انتخاب فالبکی که در ۸ پیام اخیر استفاده نشده باشد
        unused = [f for f in pool if f not in self._recent_replies]
        return random.choice(unused or pool)
