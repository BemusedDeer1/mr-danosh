# 🎓 ربات تلگرام «آقای دانشور PRO»

همدم هوشمند و طنز گروه تلگرامی با شخصیت **آقای دانشور** (معاون سابق مدرسه و معلم آمادگی دفاعی پایه نهم)، مجهز به حافظه سه‌لایه پایدار (SQLite WAL + FTS5)، شناخت تدریجی اعضای گروه و کنترل کامل حریم خصوصی.

---

## ۱. ساخت ربات و دریافت کلیدها

1. **ساخت ربات در تلگرام:**
   - به `@BotFather` پیام دهید و دستور `/newbot` را بزنید.
   - نام و یوزرنیم ربات را انتخاب کنید و `BOT_TOKEN` را کپی کنید.
   - **تنظیم حیاتی برای گروه‌ها (Group Privacy):** وارد `/mybots` شوید، ربات را انتخاب کنید، به مسیر `Bot Settings` -> `Group Privacy` بروید و آن را روی **Turn off** قرار دهید (یا ربات را در گروه Admin کنید). *توجه: اگر ربات قبلاً در گروه بوده، پس از تغییر Privacy Mode یک بار ربات را از گروه حذف و دوباره اضافه کنید تا تغییر اعمال شود.*
2. **دریافت کلید Gemini:**
   - وارد پنل [Google AI Studio](https://aistudio.google.com/) شوید و یک `GEMINI_API_KEY` بسازید.
   - نام مدل پیشنهادی سبک و سریع (`gemini-2.5-flash` یا مدل‌های Flash جدید طبق مستندات گوگل) را در متغیر `AI_MODEL` قرار دهید.

---

## ۲. نصب و اجرای محلی

```bash
git clone <your-repo-url> daneshvar-bot
cd daneshvar-bot
python -m venv venv
source venv/bin/activate  # در ویندوز: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
