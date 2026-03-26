import asyncio
import logging
import json
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand
import aiohttp

# ─────────────────────────────────────────
#  ⚙️  إعدادات
# ─────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8688561478:AAGRQ2a2qujKiRVHlfWck_bBvEw3NxDHhe0")
CHAT_ID   = os.getenv("CHAT_ID",   "-1003767484597")

CHECK_INTERVAL = 60   # ثانية

# معرّفات التقويمات على Mosaic
CALENDAR_IDS = {
    "algiers":  9,
    "oran":     7,
    "oran_vip": 8,   # ← غيّر الـ ID إذا عندك المعرّف الصحيح
}

# ─────────────────────────────────────────
#  📦  حالة المراقبة (في الذاكرة)
# ─────────────────────────────────────────
state: dict[str, bool] = {
    "algiers":  False,
    "oran":     False,
    "oran_vip": False,
}

NAMES = {
    "algiers":  "الجزائر العاصمة",
    "oran":     "وهران",
    "oran_vip": "وهران VIP",
}

# ─────────────────────────────────────────
#  🔧  إعداد اللوجر
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  🤖  إنشاء البوت
# ─────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ══════════════════════════════════════════
#  🛠️  دوال المراقبة
# ══════════════════════════════════════════

async def fetch_calendar(session: aiohttp.ClientSession, cal_id: int, month: str) -> str | None:
    """جلب HTML صفحة التقويم من Mosaic."""
    url = f"https://appointment.mosaicvisa.com/calendar/{cal_id}?month={month}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.text()
            log.warning(f"HTTP {resp.status} — calendar {cal_id} month {month}")
    except Exception as e:
        log.error(f"fetch_calendar error: {e}")
    return None


def parse_dates(html: str) -> dict[str, int]:
    """استخراج التواريخ المتاحة من HTML التقويم."""
    from html.parser import HTMLParser

    available: dict[str, int] = {}
    today = datetime.now().strftime("%Y-%m-%d")

    class _Parser(HTMLParser):
        _cur_date = ""
        _cur_rem  = 0
        _in_strong = False

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag == "tr" and "calendar-dates" in d.get("class", ""):
                self._cur_date = d.get("data-date", "").strip()
                self._cur_rem  = int(d.get("data-remaining", "0") or "0")
            if tag == "strong":
                self._in_strong = True

        def handle_data(self, data):
            if self._in_strong and self._cur_date and self._cur_date >= today and self._cur_rem > 0:
                available[data.strip()] = self._cur_rem

        def handle_endtag(self, tag):
            if tag == "strong":
                self._in_strong = False

    _Parser().feed(html)
    return available


def get_months(ahead: int = 2) -> list[str]:
    now = datetime.now()
    months = []
    for i in range(ahead):
        m = now.month + i
        y = now.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        months.append(f"{y}-{m:02d}")
    return months


async def check_algiers() -> dict[str, int]:
    """فحص مواعيد مركز الجزائر العاصمة."""
    return await _check_center("algiers")


async def check_oran() -> dict[str, int]:
    """فحص مواعيد مركز وهران."""
    return await _check_center("oran")


async def check_oran_vip() -> dict[str, int]:
    """فحص مواعيد مركز وهران VIP."""
    return await _check_center("oran_vip")


async def _check_center(key: str) -> dict[str, int]:
    """دالة مشتركة تجلب التواريخ لأي مركز."""
    cal_id = CALENDAR_IDS[key]
    all_dates: dict[str, int] = {}

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
    ) as session:
        for month in get_months():
            html = await fetch_calendar(session, cal_id, month)
            if html:
                all_dates.update(parse_dates(html))
            await asyncio.sleep(1)

    return all_dates


# ══════════════════════════════════════════
#  📱  أوامر البوت
# ══════════════════════════════════════════

def status_icon(key: str) -> str:
    return "🟢 شغال" if state[key] else "🔴 متوقف"


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    """أمر تشخيصي — يفحص الآن ويرسل النتيجة مباشرة."""
    await message.answer("🔍 جاري الفحص، انتظر...")

    for key in CALENDAR_IDS:
        try:
            cal_id = CALENDAR_IDS[key]
            month  = get_months(1)[0]

            async with aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
            ) as session:
                html = await fetch_calendar(session, cal_id, month)

            if html is None:
                await message.answer(f"❌ <b>{NAMES[key]}</b>: فشل جلب الصفحة (الموقع بلوك أو داون)", parse_mode="HTML")
                continue

            dates = parse_dates(html)

            if dates:
                lines = "\n".join(f"  • {d} — {s} مكان" for d, s in sorted(dates.items()))
                await message.answer(
                    f"✅ <b>{NAMES[key]}</b>:\n{lines}",
                    parse_mode="HTML"
                )
            else:
                # أرسل أول 500 حرف من الـ HTML للتشخيص
                preview = html[:500].replace("<", "&lt;").replace(">", "&gt;")
                await message.answer(
                    f"📭 <b>{NAMES[key]}</b>: لا مواعيد — أول الـ HTML:\n<pre>{preview}</pre>",
                    parse_mode="HTML"
                )

        except Exception as e:
            await message.answer(f"💥 <b>{NAMES[key]}</b>: {e}", parse_mode="HTML")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = (
        "🇩🇿 <b>Mosaic Visa Monitor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 الجزائر العاصمة : {status_icon('algiers')}\n"
        f"📍 وهران             : {status_icon('oran')}\n"
        f"📍 وهران VIP         : {status_icon('oran_vip')}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>الأوامر المتاحة:</b>\n"
        "/algiers_on  — /algiers_off\n"
        "/oran_on  — /oran_off\n"
        "/oran_vip_on  — /oran_vip_off"
    )
    await message.answer(text, parse_mode="HTML")


# ── الجزائر العاصمة ──────────────────────

@dp.message(Command("algiers_on"))
async def cmd_algiers_on(message: types.Message):
    state["algiers"] = True
    log.info("algiers → ON")
    await message.answer("✅ تم تشغيل مراقبة مركز <b>الجزائر العاصمة</b>", parse_mode="HTML")


@dp.message(Command("algiers_off"))
async def cmd_algiers_off(message: types.Message):
    state["algiers"] = False
    log.info("algiers → OFF")
    await message.answer("❌ تم إيقاف مراقبة مركز <b>الجزائر العاصمة</b>", parse_mode="HTML")


# ── وهران ─────────────────────────────────

@dp.message(Command("oran_on"))
async def cmd_oran_on(message: types.Message):
    state["oran"] = True
    log.info("oran → ON")
    await message.answer("✅ تم تشغيل مراقبة مركز <b>وهران</b>", parse_mode="HTML")


@dp.message(Command("oran_off"))
async def cmd_oran_off(message: types.Message):
    state["oran"] = False
    log.info("oran → OFF")
    await message.answer("❌ تم إيقاف مراقبة مركز <b>وهران</b>", parse_mode="HTML")


# ── وهران VIP ─────────────────────────────

@dp.message(Command("oran_vip_on"))
async def cmd_oran_vip_on(message: types.Message):
    state["oran_vip"] = True
    log.info("oran_vip → ON")
    await message.answer("✅ تم تشغيل مراقبة مركز <b>وهران VIP</b>", parse_mode="HTML")


@dp.message(Command("oran_vip_off"))
async def cmd_oran_vip_off(message: types.Message):
    state["oran_vip"] = False
    log.info("oran_vip → OFF")
    await message.answer("❌ تم إيقاف مراقبة مركز <b>وهران VIP</b>", parse_mode="HTML")


# ══════════════════════════════════════════
#  🔁  Loop المراقبة
# ══════════════════════════════════════════

CHECKERS = {
    "algiers":  check_algiers,
    "oran":     check_oran,
    "oran_vip": check_oran_vip,
}


async def monitor_loop():
    """يعمل كل CHECK_INTERVAL ثانية ويفحص المراكز الـ ON فقط."""
    await asyncio.sleep(5)   # انتظر حتى يبدأ البوت

    while True:
        active = [k for k, v in state.items() if v]

        if active:
            log.info(f"🔍 فحص: {', '.join(active)}")
        else:
            log.info("💤 جميع المراكز متوقفة")

        for key in active:
            try:
                dates = await CHECKERS[key]()

                if dates:
                    log.info(f"🚨 {NAMES[key]}: {len(dates)} موعد متاح — جاري الإرسال")
                    await _send_alert(key, dates)
                else:
                    log.info(f"📭 {NAMES[key]}: لا توجد مواعيد")

            except Exception as e:
                log.error(f"خطأ في فحص {key}: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def _send_alert(key: str, dates: dict[str, int]):
    """إرسال إشعار تلغرام عند وجود مواعيد."""
    cal_id = CALENDAR_IDS[key]
    lines  = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    text   = (
        f"🚨🚨🚨 <b>مواعيد متاحة!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n\n"
        f"📅 <b>التواريخ:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="📅 احجز الآن",
            url=f"https://appointment.mosaicvisa.com/calendar/{cal_id}"
        )
    ]])
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error(f"فشل إرسال الإشعار: {e}")


# ══════════════════════════════════════════
#  🚀  نقطة الدخول
# ══════════════════════════════════════════

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start",        description="حالة البوت"),
        BotCommand(command="check",        description="فحص فوري + تشخيص"),
        BotCommand(command="algiers_on",   description="تشغيل الجزائر العاصمة"),
        BotCommand(command="algiers_off",  description="إيقاف الجزائر العاصمة"),
        BotCommand(command="oran_on",      description="تشغيل وهران"),
        BotCommand(command="oran_off",     description="إيقاف وهران"),
        BotCommand(command="oran_vip_on",  description="تشغيل وهران VIP"),
        BotCommand(command="oran_vip_off", description="إيقاف وهران VIP"),
    ])


async def main():
    log.info("🚀 Mosaic Bot يبدأ...")
    await set_commands()
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
