import asyncio
import logging
import os
from datetime import datetime
from typing import Callable, Awaitable, Any

from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BotCommand
import aiohttp

# ─────────────────────────────────────────
#  ⚙️  إعدادات
# ─────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN", "8688561478:AAGRQ2a2qujKiRVHlfWck_bBvEw3NxDHhe0")
CHAT_ID    = os.getenv("CHAT_ID",   "-1003680423989")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "8499305437"))

CONSTANTINE_THRESHOLD = 10  # إشعار قسنطينة لما المجموع ينزل تحت هذا الرقم

# ✅ حد النقصان لكل مركز (يرسل لما تنقص هذا العدد أو أكثر دفعة وحدة)
# 0 = الخاصية معطلة لهذا المركز
DROP_THRESHOLDS: dict[str, int] = {
    "algiers":      5,
    "constantine":  5,
    "oran":         5,
    "oran_vip":     5,
}

CHECK_INTERVALS: dict[str, int] = {
    "algiers":      60,
    "constantine":  60,
    "oran":         3600,
    "oran_vip":     3600,
}

CALENDAR_IDS = {
    "algiers":      9,
    "constantine":  17,
    "oran":         7,
    "oran_vip":     8,
}

state: dict[str, bool] = {
    "algiers":      False,
    "constantine":  False,
    "oran":         False,
    "oran_vip":     False,
}

last_checked: dict[str, float] = {
    "algiers":      0.0,
    "constantine":  0.0,
    "oran":         0.0,
    "oran_vip":     0.0,
}

# ✅ يحفظ آخر مجموع مواعيد لكل مركز لمقارنة النقصان
last_total: dict[str, int | None] = {
    "algiers":      None,
    "constantine":  None,
    "oran":         None,
    "oran_vip":     None,
}

constantine_alert_sent: bool = False

NAMES = {
    "algiers":      "الجزائر العاصمة",
    "constantine":  "قسنطينة",
    "oran":         "وهران",
    "oran_vip":     "وهران VIP",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ══════════════════════════════════════════
#  🔒  Middleware
# ══════════════════════════════════════════
class AdminPrivateOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.Message, dict], Awaitable[Any]],
        event: types.Message,
        data: dict
    ) -> Any:
        if event.chat.type == "private" and event.from_user.id == ADMIN_ID:
            return await handler(event, data)
        log.info(f"🚫 رسالة مرفوضة — chat: {event.chat.id}, user: {event.from_user.id}")
        return

dp.message.middleware(AdminPrivateOnlyMiddleware())


# ══════════════════════════════════════════
#  🛠️  دوال المراقبة
# ══════════════════════════════════════════

async def fetch_calendar(session: aiohttp.ClientSession, cal_id: int, month: str) -> str | None:
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
    from html.parser import HTMLParser
    available: dict[str, int] = {}
    today = datetime.now().strftime("%Y-%m-%d")

    class _Parser(HTMLParser):
        _cur_date  = ""
        _cur_rem   = 0
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


async def _check_center(key: str) -> dict[str, int]:
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


async def check_algiers()     -> dict[str, int]: return await _check_center("algiers")
async def check_constantine() -> dict[str, int]: return await _check_center("constantine")
async def check_oran()        -> dict[str, int]: return await _check_center("oran")
async def check_oran_vip()    -> dict[str, int]: return await _check_center("oran_vip")


# ══════════════════════════════════════════
#  🔧  دوال مساعدة
# ══════════════════════════════════════════

def interval_label(key: str) -> str:
    secs = CHECK_INTERVALS[key]
    if secs < 60:     return f"{secs}ث"
    elif secs < 3600: return f"{secs // 60}د"
    else:             return f"{secs // 3600}س"

def status_icon(key: str) -> str:
    return "🟢 شغال" if state[key] else "🔴 متوقف"

def _fmt_secs(secs: int) -> str:
    if secs < 60:     return f"{secs} ثانية"
    elif secs < 3600: return f"{secs // 60} دقيقة"
    else:             return f"{secs // 3600} ساعة"

def parse_interval(value: str) -> int | None:
    value = value.strip().lower()
    try:
        if value.endswith("h"):   return int(value[:-1]) * 3600
        elif value.endswith("m"): return int(value[:-1]) * 60
        elif value.endswith("s"): return int(value[:-1])
        else:                     return int(value)
    except ValueError:
        return None


# ══════════════════════════════════════════
#  📱  أوامر البوت
# ══════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = (
        "🇩🇿 <b>Mosaic Visa Monitor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 الجزائر العاصمة : {status_icon('algiers')} — كل <b>{interval_label('algiers')}</b> — نقصان ≥ {DROP_THRESHOLDS['algiers']}\n"
        f"📍 قسنطينة          : {status_icon('constantine')} — كل <b>{interval_label('constantine')}</b> — إشعار &lt;{CONSTANTINE_THRESHOLD} — نقصان ≥ {DROP_THRESHOLDS['constantine']}\n"
        f"📍 وهران             : {status_icon('oran')} — كل <b>{interval_label('oran')}</b> — نقصان ≥ {DROP_THRESHOLDS['oran']}\n"
        f"📍 وهران VIP         : {status_icon('oran_vip')} — كل <b>{interval_label('oran_vip')}</b> — نقصان ≥ {DROP_THRESHOLDS['oran_vip']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>أوامر التشغيل/الإيقاف:</b>\n"
        "/algiers_on  — /algiers_off\n"
        "/constantine_on  — /constantine_off\n"
        "/oran_on  — /oran_off\n"
        "/oran_vip_on  — /oran_vip_off\n\n"
        "<b>تغيير التوقيت:</b>\n"
        "<code>/interval algiers 30</code>\n"
        "<code>/interval oran 5m</code>\n\n"
        "<b>تغيير حد النقصان:</b>\n"
        "<code>/drop algiers 5</code>\n"
        "<code>/drop oran_vip 10</code>\n"
        "<code>/drop constantine 0</code>  ← لتعطيل\n\n"
        "/intervals — عرض التواقيت\n"
        "/drops — عرض حدود النقصان\n"
        "/check — فحص فوري"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("intervals"))
async def cmd_intervals(message: types.Message):
    text = (
        "⏱ <b>التواقيت الحالية:</b>\n\n"
        f"📍 algiers      → كل <b>{interval_label('algiers')}</b> ({CHECK_INTERVALS['algiers']}ث)\n"
        f"📍 constantine  → كل <b>{interval_label('constantine')}</b> ({CHECK_INTERVALS['constantine']}ث)\n"
        f"📍 oran         → كل <b>{interval_label('oran')}</b> ({CHECK_INTERVALS['oran']}ث)\n"
        f"📍 oran_vip     → كل <b>{interval_label('oran_vip')}</b> ({CHECK_INTERVALS['oran_vip']}ث)\n\n"
        "<i>لتغيير: /interval [مكان] [قيمة]</i>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("drops"))
async def cmd_drops(message: types.Message):
    def drop_label(key: str) -> str:
        v = DROP_THRESHOLDS[key]
        return f"<b>{v}+</b> مكان" if v > 0 else "🔕 معطل"

    text = (
        "📉 <b>حدود النقصان الحالية:</b>\n\n"
        f"📍 algiers      → {drop_label('algiers')}\n"
        f"📍 constantine  → {drop_label('constantine')}\n"
        f"📍 oran         → {drop_label('oran')}\n"
        f"📍 oran_vip     → {drop_label('oran_vip')}\n\n"
        "<i>لتغيير: /drop [مكان] [رقم]  (0 = تعطيل)</i>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("interval"))
async def cmd_interval(message: types.Message):
    args = (message.text or "").split()[1:]
    if len(args) != 2:
        await message.answer(
            "⚠️ <b>الاستخدام:</b> <code>/interval [مكان] [قيمة]</code>\n"
            "مثال: <code>/interval algiers 2m</code>",
            parse_mode="HTML"
        )
        return

    key, raw_value = args[0].lower(), args[1]
    if key not in CHECK_INTERVALS:
        await message.answer(
            f"❌ مكان غير معروف: <code>{key}</code>\n"
            f"المتاح: algiers / constantine / oran / oran_vip",
            parse_mode="HTML"
        )
        return

    secs = parse_interval(raw_value)
    if secs is None or secs < 10:
        await message.answer("❌ قيمة غير صالحة. يجب أن تكون ≥ 10 ثواني.", parse_mode="HTML")
        return

    old = CHECK_INTERVALS[key]
    CHECK_INTERVALS[key] = secs
    last_checked[key] = 0.0

    await message.answer(
        f"✅ توقيت <b>{NAMES[key]}</b>:\n"
        f"  {_fmt_secs(old)} ← <b>{_fmt_secs(secs)}</b>",
        parse_mode="HTML"
    )
    log.info(f"⏱ توقيت {key}: {old}ث → {secs}ث")


@dp.message(Command("drop"))
async def cmd_drop(message: types.Message):
    """
    تغيير حد النقصان لمركز معين.
    /drop algiers 5   → يرسل لما تنقص 5 مواعيد أو أكثر
    /drop oran 0      → تعطيل الخاصية لهذا المركز
    """
    args = (message.text or "").split()[1:]
    if len(args) != 2:
        await message.answer(
            "⚠️ <b>الاستخدام:</b> <code>/drop [مكان] [رقم]</code>\n\n"
            "مثال: <code>/drop algiers 5</code>\n"
            "<code>/drop oran 0</code>  ← تعطيل",
            parse_mode="HTML"
        )
        return

    key = args[0].lower()
    if key not in DROP_THRESHOLDS:
        await message.answer(
            f"❌ مكان غير معروف: <code>{key}</code>\n"
            f"المتاح: algiers / constantine / oran / oran_vip",
            parse_mode="HTML"
        )
        return

    try:
        value = int(args[1])
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرقم يجب أن يكون 0 أو أكثر.", parse_mode="HTML")
        return

    old = DROP_THRESHOLDS[key]
    DROP_THRESHOLDS[key] = value
    # نصفر last_total حتى لا يقارن بقيم قديمة
    last_total[key] = None

    if value == 0:
        await message.answer(
            f"🔕 تم <b>تعطيل</b> إشعار النقصان لـ <b>{NAMES[key]}</b>",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"📉 حد النقصان لـ <b>{NAMES[key]}</b>:\n"
            f"  {'معطل' if old == 0 else f'{old}+'} ← <b>{value}+ مكان</b>",
            parse_mode="HTML"
        )
    log.info(f"📉 حد النقصان {key}: {old} → {value}")


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
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
                await message.answer(f"❌ <b>{NAMES[key]}</b>: فشل جلب الصفحة", parse_mode="HTML")
                continue
            dates = parse_dates(html)
            total = sum(dates.values())
            if dates:
                lines = "\n".join(f"  • {d} — {s} مكان" for d, s in sorted(dates.items()))
                await message.answer(
                    f"✅ <b>{NAMES[key]}</b> — المجموع: <b>{total} مكان</b>\n{lines}",
                    parse_mode="HTML"
                )
            else:
                await message.answer(f"📭 <b>{NAMES[key]}</b>: لا مواعيد", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"💥 <b>{NAMES[key]}</b>: {e}", parse_mode="HTML")


@dp.message(Command("algiers_on"))
async def cmd_algiers_on(message: types.Message):
    state["algiers"] = True
    await message.answer("✅ تم تشغيل مراقبة <b>الجزائر العاصمة</b>", parse_mode="HTML")

@dp.message(Command("algiers_off"))
async def cmd_algiers_off(message: types.Message):
    state["algiers"] = False
    await message.answer("❌ تم إيقاف مراقبة <b>الجزائر العاصمة</b>", parse_mode="HTML")

@dp.message(Command("constantine_on"))
async def cmd_constantine_on(message: types.Message):
    state["constantine"] = True
    await message.answer("✅ تم تشغيل مراقبة <b>قسنطينة</b>", parse_mode="HTML")

@dp.message(Command("constantine_off"))
async def cmd_constantine_off(message: types.Message):
    state["constantine"] = False
    await message.answer("❌ تم إيقاف مراقبة <b>قسنطينة</b>", parse_mode="HTML")

@dp.message(Command("oran_on"))
async def cmd_oran_on(message: types.Message):
    state["oran"] = True
    await message.answer("✅ تم تشغيل مراقبة <b>وهران</b>", parse_mode="HTML")

@dp.message(Command("oran_off"))
async def cmd_oran_off(message: types.Message):
    state["oran"] = False
    await message.answer("❌ تم إيقاف مراقبة <b>وهران</b>", parse_mode="HTML")

@dp.message(Command("oran_vip_on"))
async def cmd_oran_vip_on(message: types.Message):
    state["oran_vip"] = True
    await message.answer("✅ تم تشغيل مراقبة <b>وهران VIP</b>", parse_mode="HTML")

@dp.message(Command("oran_vip_off"))
async def cmd_oran_vip_off(message: types.Message):
    state["oran_vip"] = False
    await message.answer("❌ تم إيقاف مراقبة <b>وهران VIP</b>", parse_mode="HTML")


# ══════════════════════════════════════════
#  🔁  Loop المراقبة
# ══════════════════════════════════════════

CHECKERS = {
    "algiers":      check_algiers,
    "constantine":  check_constantine,
    "oran":         check_oran,
    "oran_vip":     check_oran_vip,
}


async def monitor_loop():
    global constantine_alert_sent
    await asyncio.sleep(5)

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔄 <b>تم تحديث البوت بنجاح!</b>\n\n"
                f"📍 الجزائر العاصمة : كل {interval_label('algiers')} — نقصان ≥ {DROP_THRESHOLDS['algiers']}\n"
                f"📍 قسنطينة          : كل {interval_label('constantine')} — إشعار &lt;{CONSTANTINE_THRESHOLD} — نقصان ≥ {DROP_THRESHOLDS['constantine']}\n"
                f"📍 وهران             : كل {interval_label('oran')} — نقصان ≥ {DROP_THRESHOLDS['oran']}\n"
                f"📍 وهران VIP         : كل {interval_label('oran_vip')} — نقصان ≥ {DROP_THRESHOLDS['oran_vip']}\n\n"
                f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"فشل إرسال رسالة التحديث: {e}")

    while True:
        now    = asyncio.get_event_loop().time()
        active = [k for k, v in state.items() if v]

        if active:
            log.info(f"🔍 فحص دوري: {', '.join(active)}")
        else:
            log.info("💤 جميع المراكز متوقفة")

        for key in active:
            elapsed = now - last_checked[key]
            if elapsed < CHECK_INTERVALS[key]:
                remaining = int(CHECK_INTERVALS[key] - elapsed)
                log.info(f"⏳ {NAMES[key]}: متبقي {remaining}ث للفحص القادم")
                continue

            try:
                dates = await CHECKERS[key]()
                last_checked[key] = asyncio.get_event_loop().time()
                total = sum(dates.values())
                log.info(f"📊 {NAMES[key]}: {total} مكان في {len(dates)} تاريخ")

                # ── 1. إشعار النقصان (لكل المراكز) ──
                drop_threshold = DROP_THRESHOLDS[key]
                prev = last_total[key]

                if drop_threshold > 0 and prev is not None:
                    drop = prev - total
                    if drop >= drop_threshold:
                        log.info(f"📉 {NAMES[key]}: نقص {drop} مكان ({prev} → {total}) — إرسال إشعار")
                        await _send_drop_alert(key, dates, total, prev, drop)
                    elif drop > 0:
                        log.info(f"📉 {NAMES[key]}: نقص {drop} مكان ({prev} → {total}) — أقل من الحد ({drop_threshold})")

                last_total[key] = total

                # ── 2. منطق قسنطينة الخاص (أقل من threshold) ──
                if key == "constantine":
                    if 0 < total < CONSTANTINE_THRESHOLD:
                        if not constantine_alert_sent:
                            log.info(f"🚨 قسنطينة: {total} مكان < {CONSTANTINE_THRESHOLD} — إرسال إشعار")
                            await _send_alert_threshold(dates, total)
                            constantine_alert_sent = True
                        else:
                            log.info(f"🔕 قسنطينة: {total} مكان — إشعار سبق إرساله")
                    elif total >= CONSTANTINE_THRESHOLD:
                        if constantine_alert_sent:
                            log.info(f"✅ قسنطينة: ارتفع لـ {total} — تم تصفير flag")
                            constantine_alert_sent = False
                    else:
                        if constantine_alert_sent:
                            constantine_alert_sent = False
                            log.info("🔄 قسنطينة: لا مواعيد — تم تصفير flag")

                # ── 3. المنطق الأصلي للمراكز الأخرى (أي مواعيد) ──
                else:
                    if dates:
                        log.info(f"🚨 {NAMES[key]}: {len(dates)} موعد — إرسال إشعار")
                        await _send_alert(key, dates)
                    else:
                        log.info(f"📭 {NAMES[key]}: لا توجد مواعيد")

            except Exception as e:
                log.error(f"خطأ في فحص {key}: {e}")

        await asyncio.sleep(60)


# ══════════════════════════════════════════
#  📣  دوال الإرسال
# ══════════════════════════════════════════

async def _send_alert(key: str, dates: dict[str, int]):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    text  = (
        f"🚨🚨🚨 <b>مواعيد متاحة!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n\n"
        f"📅 <b>التواريخ:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )
    await _send_to_group(key, text)


async def _send_alert_threshold(dates: dict[str, int], total: int):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    text  = (
        f"⚠️⚠️⚠️ <b>مواعيد قسنطينة تنفد!</b>\n\n"
        f"📍 <b>قسنطينة</b>\n"
        f"🪑 المجموع: <b>{total} مكان فقط</b>\n\n"
        f"📅 <b>التواريخ المتاحة:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )
    await _send_to_group("constantine", text)


async def _send_drop_alert(key: str, dates: dict[str, int], total: int, prev: int, drop: int):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    text  = (
        f"📉📉 <b>نقصت المواعيد!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n"
        f"🔻 نقص: <b>{drop} مكان</b>  ({prev} ← <b>{total}</b>)\n\n"
        f"📅 <b>المواعيد المتبقية:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )
    await _send_to_group(key, text)


async def _send_to_group(key: str, text: str):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="📅 احجز الآن",
            url=f"https://appointment.mosaicvisa.com/calendar/{CALENDAR_IDS[key]}"
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
        BotCommand(command="start",           description="حالة البوت"),
        BotCommand(command="check",           description="فحص فوري"),
        BotCommand(command="intervals",       description="عرض التواقيت"),
        BotCommand(command="interval",        description="تغيير توقيت مركز"),
        BotCommand(command="drops",           description="عرض حدود النقصان"),
        BotCommand(command="drop",            description="تغيير حد النقصان"),
        BotCommand(command="algiers_on",      description="تشغيل الجزائر العاصمة"),
        BotCommand(command="algiers_off",     description="إيقاف الجزائر العاصمة"),
        BotCommand(command="constantine_on",  description="تشغيل قسنطينة"),
        BotCommand(command="constantine_off", description="إيقاف قسنطينة"),
        BotCommand(command="oran_on",         description="تشغيل وهران"),
        BotCommand(command="oran_off",        description="إيقاف وهران"),
        BotCommand(command="oran_vip_on",     description="تشغيل وهران VIP"),
        BotCommand(command="oran_vip_off",    description="إيقاف وهران VIP"),
    ])


async def main():
    log.info("🚀 Mosaic Bot يبدأ...")
    await set_commands()
    asyncio.create_task(monitor_loop())
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
