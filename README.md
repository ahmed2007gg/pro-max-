# 🇩🇿 Mosaic Visa Monitor Bot

بوت تلغرام لمراقبة مواعيد Mosaic Visa، مبني بـ Python + aiogram 3.

---

## 📁 هيكل الملفات

```
mosaic_bot/
├── bot.py            ← الكود الرئيسي
├── requirements.txt  ← المكتبات
├── Procfile          ← للنشر على Railway
└── .env.example      ← مثال المتغيرات
```

---

## ⚙️ الإعداد المحلي

```bash
# 1. تثبيت المكتبات
pip install -r requirements.txt

# 2. إنشاء ملف .env
cp .env.example .env
# ثم افتح .env وأدخل BOT_TOKEN و CHAT_ID

# 3. تشغيل البوت
python bot.py
```

---

## 🚀 النشر على Railway

1. ارفع المجلد على GitHub
2. اذهب إلى [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. أضف المتغيرات في **Variables**:
   - `BOT_TOKEN` = توكن البوت من @BotFather
   - `CHAT_ID`   = معرّف المحادثة (Chat ID)
4. Railway يقرأ `Procfile` تلقائياً ويشغّل `python bot.py`

---

## 📋 الأوامر

| الأمر           | الوظيفة                        |
|-----------------|-------------------------------|
| `/start`        | عرض حالة جميع المراكز         |
| `/algiers_on`   | تشغيل مراقبة الجزائر العاصمة  |
| `/algiers_off`  | إيقاف مراقبة الجزائر العاصمة  |
| `/oran_on`      | تشغيل مراقبة وهران            |
| `/oran_off`     | إيقاف مراقبة وهران            |
| `/oran_vip_on`  | تشغيل مراقبة وهران VIP        |
| `/oran_vip_off` | إيقاف مراقبة وهران VIP        |

---

## 🔧 تخصيص

في `bot.py` عدّل:
- `CHECK_INTERVAL` : فترة الفحص بالثواني (افتراضي 60)
- `CALENDAR_IDS`   : معرّفات التقويمات على Mosaic
