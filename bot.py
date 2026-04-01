import asyncio
import logging
import os
import json
from datetime import datetime, time as dtime
from typing import Callable, Awaitable, Any

from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BotCommand
import aiohttp

# ─────────────────────────────────────────
#  ⚙️  إعدادات
# ─────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN", "8688561478:AAFCC3OoJFyMOtAN4gseG_E-qvIsGpq-dgs")
CHAT_ID    = os.getenv("CHAT_ID",  "-1003837495585")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "8499305437"))
STATS_FILE = "stats.json"

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

state: dict[str, bool] = {k: False for k in CALENDAR_IDS}
last_checked: dict[str, float] = {k: 0.0 for k in CALENDAR_IDS}
last_total: dict[str, int | None] = {k: None for k in CALENDAR_IDS}

# ── ساعات صامتة (24h) ──
quiet_start: int = 0
quiet_end:   int = 0

# ── إيقاف مؤقت ──
pause_until: float = 0.0

# ── Heartbeat ──
heartbeat_interval: int = 0
last_heartbeat: float   = 0.0

# ── إحصائيات ──
stats: dict = {}

# ── فشل الموقع ──
consecutive_failures: dict[str, int] = {k: 0 for k in CALENDAR_IDS}
FAILURE_ALERT_AFTER = 3

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
#  📊  إدارة الإحصائيات
# ══════════════════════════════════════════

def _load_stats():
    global stats
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            stats = {}
    _init_stats()

def _init_stats():
    for key in CALENDAR_IDS:
        if key not in stats:
            stats[key] = {
                "alerts_sent":    0,
                "drop_alerts":    0,
                "rise_alerts":    0,
                "last_alert":     None,
                "peak_total":     0,
                "lowest_total":   None,
                "checks_done":    0,
            }

def _save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"فشل حفظ الإحصائيات: {e}")

def _update_stats(key: str, total: int, alert_type: str | None = None):
    s = stats[key]
    s["checks_done"] += 1
    if total > s["peak_total"]:
        s["peak_total"] = total
    if s["lowest_total"] is None or (total > 0 and total < s["lowest_total"]):
        s["lowest_total"] = total
    if alert_type == "alert":
        s["alerts_sent"] += 1
        s["last_alert"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    elif alert_type == "drop":
        s["drop_alerts"] += 1
        s["last_alert"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    elif alert_type == "rise":
        s["rise_alerts"] += 1
        s["last_alert"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    _save_stats()


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


async def _check_center(key: str) -> dict[str, int] | None:
    cal_id = CALENDAR_IDS[key]
    all_dates: dict[str, int] = {}
    failed = True
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
    ) as session:
        for month in get_months():
            html = await fetch_calendar(session, cal_id, month)
            if html is not None:
                all_dates.update(parse_dates(html))
                failed = False
            await asyncio.sleep(1)
    return None if failed else all_dates


async def check_algiers()     : return await _check_center("algiers")
async def check_constantine() : return await _check_center("constantine")
async def check_oran()        : return await _check_center("oran")
async def check_oran_vip()    : return await _check_center("oran_vip")


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

def is_quiet_time() -> bool:
    if quiet_start == 0 and quiet_end == 0:
        return False
    now_h = datetime.now().hour
    if quiet_start < quiet_end:
        return quiet_start <= now_h < quiet_end
    else:
        return now_h >= quiet_start or now_h < quiet_end

def is_paused() -> bool:
    return asyncio.get_event_loop().time() < pause_until


# ══════════════════════════════════════════
#  📱  أوامر البوت
# ══════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    quiet_str = f"{quiet_start:02d}:00 → {quiet_end:02d}:00" if (quiet_start or quiet_end) else "معطلة"
    hb_str    = _fmt_secs(heartbeat_interval) if heartbeat_interval else "معطل"
    text = (
        "🇩🇿 <b>Mosaic Visa Monitor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 الجزائر  : {status_icon('algiers')} — {interval_label('algiers')} — نقصان≥{DROP_THRESHOLDS['algiers']}\n"
        f"📍 قسنطينة  : {status_icon('constantine')} — {interval_label('constantine')} — نقصان≥{DROP_THRESHOLDS['constantine']}\n"
        f"📍 وهران    : {status_icon('oran')} — {interval_label('oran')} — نقصان≥{DROP_THRESHOLDS['oran']}\n"
        f"📍 وهران VIP: {status_icon('oran_vip')} — {interval_label('oran_vip')} — نقصان≥{DROP_THRESHOLDS['oran_vip']}\n\n"
        f"🌙 ساعات صامتة : {quiet_str}\n"
        f"💓 Heartbeat    : {hb_str}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>تشغيل / إيقاف:</b>\n"
        "/algiers_on · /algiers_off\n"
        "/constantine_on · /constantine_off\n"
        "/oran_on · /oran_off\n"
        "/oran_vip_on · /oran_vip_off\n\n"
        "<b>إعدادات:</b>\n"
        "/interval [مكان] [قيمة]  — توقيت الفحص\n"
        "/drop [مكان] [رقم]       — حد النقصان\n"
        "/quiet [بداية] [نهاية]   — ساعات صامتة\n"
        "/pause [مدة]             — إيقاف مؤقت\n"
        "/heartbeat [مدة]         — نبض التأكيد\n\n"
        "<b>معلومات:</b>\n"
        "/intervals · /drops · /stats · /check\n"
        "/reset [مكان|all]        — إعادة تعيين إحصائيات\n"
        "/daily — تقرير يومي فوري"
    )
    await message.answer(text, parse_mode="HTML")


# ── تشغيل / إيقاف ──
@dp.message(Command("algiers_on"))
async def cmd_algiers_on(msg: types.Message):
    state["algiers"] = True
    await msg.answer("✅ تم تشغيل <b>الجزائر العاصمة</b>", parse_mode="HTML")

@dp.message(Command("algiers_off"))
async def cmd_algiers_off(msg: types.Message):
    state["algiers"] = False
    await msg.answer("❌ تم إيقاف <b>الجزائر العاصمة</b>", parse_mode="HTML")

@dp.message(Command("constantine_on"))
async def cmd_constantine_on(msg: types.Message):
    state["constantine"] = True
    await msg.answer("✅ تم تشغيل <b>قسنطينة</b>", parse_mode="HTML")

@dp.message(Command("constantine_off"))
async def cmd_constantine_off(msg: types.Message):
    state["constantine"] = False
    await msg.answer("❌ تم إيقاف <b>قسنطينة</b>", parse_mode="HTML")

@dp.message(Command("oran_on"))
async def cmd_oran_on(msg: types.Message):
    state["oran"] = True
    await msg.answer("✅ تم تشغيل <b>وهران</b>", parse_mode="HTML")

@dp.message(Command("oran_off"))
async def cmd_oran_off(msg: types.Message):
    state["oran"] = False
    await msg.answer("❌ تم إيقاف <b>وهران</b>", parse_mode="HTML")

@dp.message(Command("oran_vip_on"))
async def cmd_oran_vip_on(msg: types.Message):
    state["oran_vip"] = True
    await msg.answer("✅ تم تشغيل <b>وهران VIP</b>", parse_mode="HTML")

@dp.message(Command("oran_vip_off"))
async def cmd_oran_vip_off(msg: types.Message):
    state["oran_vip"] = False
    await msg.answer("❌ تم إيقاف <b>وهران VIP</b>", parse_mode="HTML")


# ── التواقيت ──
@dp.message(Command("intervals"))
async def cmd_intervals(msg: types.Message):
    text = (
        "⏱ <b>التواقيت الحالية:</b>\n\n"
        + "\n".join(
            f"📍 {k:12} → <b>{interval_label(k)}</b> ({CHECK_INTERVALS[k]}ث)"
            for k in CALENDAR_IDS
        )
        + "\n\n<i>/interval [مكان] [قيمة]  مثال: /interval algiers 2m</i>"
    )
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("interval"))
async def cmd_interval(msg: types.Message):
    args = (msg.text or "").split()[1:]
    if len(args) != 2:
        await msg.answer("⚠️ <code>/interval [مكان] [قيمة]</code>\nمثال: <code>/interval algiers 2m</code>", parse_mode="HTML")
        return
    key, raw = args[0].lower(), args[1]
    if key not in CHECK_INTERVALS:
        await msg.answer(f"❌ مكان غير معروف: <code>{key}</code>", parse_mode="HTML"); return
    secs = parse_interval(raw)
    if not secs or secs < 10:
        await msg.answer("❌ قيمة غير صالحة. يجب ≥ 10 ثواني.", parse_mode="HTML"); return
    old = CHECK_INTERVALS[key]
    CHECK_INTERVALS[key] = secs
    last_checked[key] = 0.0
    await msg.answer(f"✅ توقيت <b>{NAMES[key]}</b>: {_fmt_secs(old)} ← <b>{_fmt_secs(secs)}</b>", parse_mode="HTML")


# ── حدود النقصان ──
@dp.message(Command("drops"))
async def cmd_drops(msg: types.Message):
    def lbl(k): v = DROP_THRESHOLDS[k]; return f"<b>{v}+</b>" if v else "🔕 معطل"
    text = "📉 <b>حدود النقصان:</b>\n\n" + "\n".join(f"📍 {k:12} → {lbl(k)}" for k in CALENDAR_IDS)
    text += "\n\n<i>/drop [مكان] [رقم]  (0 = تعطيل)</i>"
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("drop"))
async def cmd_drop(msg: types.Message):
    args = (msg.text or "").split()[1:]
    if len(args) != 2:
        await msg.answer("⚠️ <code>/drop [مكان] [رقم]</code>", parse_mode="HTML"); return
    key = args[0].lower()
    if key not in DROP_THRESHOLDS:
        await msg.answer(f"❌ مكان غير معروف: <code>{key}</code>", parse_mode="HTML"); return
    try:
        value = int(args[1])
        if value < 0: raise ValueError
    except ValueError:
        await msg.answer("❌ الرقم يجب ≥ 0", parse_mode="HTML"); return
    old = DROP_THRESHOLDS[key]
    DROP_THRESHOLDS[key] = value
    last_total[key] = None
    if value == 0:
        await msg.answer(f"🔕 تم تعطيل إشعار النقصان لـ <b>{NAMES[key]}</b>", parse_mode="HTML")
    else:
        await msg.answer(f"📉 حد النقصان لـ <b>{NAMES[key]}</b>: {'معطل' if old==0 else f'{old}+'} ← <b>{value}+</b>", parse_mode="HTML")


# ── ساعات صامتة ──
@dp.message(Command("quiet"))
async def cmd_quiet(msg: types.Message):
    global quiet_start, quiet_end
    args = (msg.text or "").split()[1:]
    if len(args) == 0:
        current = f"{quiet_start:02d}:00 → {quiet_end:02d}:00" if (quiet_start or quiet_end) else "معطلة"
        await msg.answer(
            f"🌙 الساعات الصامتة الحالية: <b>{current}</b>\n\n"
            "لتغيير: <code>/quiet [بداية] [نهاية]</code>\n"
            "مثال: <code>/quiet 0 7</code>  (من منتصف الليل حتى 7 صبح)\n"
            "لتعطيل: <code>/quiet 0 0</code>",
            parse_mode="HTML"
        )
        return
    if len(args) != 2:
        await msg.answer("⚠️ <code>/quiet [بداية] [نهاية]</code>\nمثال: <code>/quiet 23 7</code>", parse_mode="HTML"); return
    try:
        s, e = int(args[0]), int(args[1])
        if not (0 <= s <= 23 and 0 <= e <= 23): raise ValueError
    except ValueError:
        await msg.answer("❌ يجب إدخال ساعات بين 0 و23", parse_mode="HTML"); return
    quiet_start, quiet_end = s, e
    if s == 0 and e == 0:
        await msg.answer("✅ تم تعطيل الساعات الصامتة", parse_mode="HTML")
    else:
        await msg.answer(f"🌙 الساعات الصامتة: <b>{s:02d}:00 → {e:02d}:00</b>", parse_mode="HTML")


# ── إيقاف مؤقت ──
@dp.message(Command("pause"))
async def cmd_pause(msg: types.Message):
    global pause_until
    args = (msg.text or "").split()[1:]
    if len(args) == 0:
        if is_paused():
            remaining = int(pause_until - asyncio.get_event_loop().time())
            await msg.answer(f"⏸ البوت موقوف مؤقتاً — متبقي <b>{_fmt_secs(remaining)}</b>\nللإلغاء: <code>/pause 0</code>", parse_mode="HTML")
        else:
            await msg.answer("▶️ البوت يعمل بشكل طبيعي.\nللإيقاف المؤقت: <code>/pause [مدة]</code>\nمثال: <code>/pause 2h</code>", parse_mode="HTML")
        return
    if args[0] == "0":
        pause_until = 0.0
        await msg.answer("▶️ تم استئناف الإشعارات", parse_mode="HTML"); return
    secs = parse_interval(args[0])
    if not secs or secs < 60:
        await msg.answer("❌ مدة غير صالحة. يجب ≥ 1 دقيقة.", parse_mode="HTML"); return
    pause_until = asyncio.get_event_loop().time() + secs
    until_str = datetime.fromtimestamp(
        datetime.now().timestamp() + secs
    ).strftime("%H:%M:%S")
    await msg.answer(f"⏸ تم إيقاف الإشعارات لـ <b>{_fmt_secs(secs)}</b> (حتى {until_str})", parse_mode="HTML")


# ── Heartbeat ──
@dp.message(Command("heartbeat"))
async def cmd_heartbeat(msg: types.Message):
    global heartbeat_interval, last_heartbeat
    args = (msg.text or "").split()[1:]
    if len(args) == 0:
        current = _fmt_secs(heartbeat_interval) if heartbeat_interval else "معطل"
        await msg.answer(
            f"💓 Heartbeat الحالي: <b>{current}</b>\n\n"
            "لتغيير: <code>/heartbeat [مدة]</code>\n"
            "مثال: <code>/heartbeat 6h</code>\n"
            "لتعطيل: <code>/heartbeat 0</code>",
            parse_mode="HTML"
        )
        return
    if args[0] == "0":
        heartbeat_interval = 0
        await msg.answer("💓 تم تعطيل Heartbeat", parse_mode="HTML"); return
    secs = parse_interval(args[0])
    if not secs or secs < 60:
        await msg.answer("❌ مدة غير صالحة. يجب ≥ 1 دقيقة.", parse_mode="HTML"); return
    heartbeat_interval = secs
    last_heartbeat = asyncio.get_event_loop().time()
    await msg.answer(f"💓 Heartbeat كل <b>{_fmt_secs(secs)}</b>", parse_mode="HTML")


# ── الإحصائيات ──
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    lines = []
    for key in CALENDAR_IDS:
        s = stats.get(key, {})
        lines.append(
            f"📍 <b>{NAMES[key]}</b>\n"
            f"   فحوصات: {s.get('checks_done', 0)}\n"
            f"   إشعارات أُرسلت: {s.get('alerts_sent', 0)}\n"
            f"   إشعارات نقصان: {s.get('drop_alerts', 0)}\n"
            f"   إشعارات ارتفاع: {s.get('rise_alerts', 0)}\n"
            f"   أعلى رصيد: {s.get('peak_total', 0)} مكان\n"
            f"   أدنى رصيد: {s.get('lowest_total') or 'N/A'} مكان\n"
            f"   آخر إشعار: {s.get('last_alert') or 'لا يوجد'}"
        )
    await msg.answer("📊 <b>الإحصائيات:</b>\n\n" + "\n\n".join(lines), parse_mode="HTML")


# ── إعادة تعيين إحصائيات ──
@dp.message(Command("reset"))
async def cmd_reset(msg: types.Message):
    args = (msg.text or "").split()[1:]
    valid_keys = list(CALENDAR_IDS.keys()) + ["all"]

    if len(args) == 0:
        keys_str = "\n".join(f"  • <code>{k}</code> — {NAMES.get(k, k)}" for k in CALENDAR_IDS)
        await msg.answer(
            "🔄 <b>إعادة تعيين الإحصائيات</b>\n\n"
            f"<i>/reset [مكان]</i>\n\n"
            f"الأماكن المتاحة:\n{keys_str}\n"
            f"  • <code>all</code> — جميع المراكز",
            parse_mode="HTML"
        )
        return

    key = args[0].lower()
    if key not in valid_keys:
        await msg.answer(f"❌ مكان غير معروف: <code>{key}</code>", parse_mode="HTML")
        return

    if key == "all":
        for k in CALENDAR_IDS:
            stats[k] = {
                "alerts_sent":  0,
                "drop_alerts":  0,
                "rise_alerts":  0,
                "last_alert":   None,
                "peak_total":   0,
                "lowest_total": None,
                "checks_done":  0,
            }
            last_total[k] = None
        _save_stats()
        await msg.answer("✅ تم إعادة تعيين إحصائيات <b>جميع المراكز</b>", parse_mode="HTML")
    else:
        stats[key] = {
            "alerts_sent":  0,
            "drop_alerts":  0,
            "rise_alerts":  0,
            "last_alert":   None,
            "peak_total":   0,
            "lowest_total": None,
            "checks_done":  0,
        }
        last_total[key] = None
        _save_stats()
        await msg.answer(f"✅ تم إعادة تعيين إحصائيات <b>{NAMES[key]}</b>", parse_mode="HTML")


# ── تقرير يومي فوري ──
@dp.message(Command("daily"))
async def cmd_daily(msg: types.Message):
    await msg.answer("📋 جاري إعداد التقرير...")
    await _send_daily_report()


# ── فحص فوري ──
@dp.message(Command("check"))
async def cmd_check(msg: types.Message):
    await msg.answer("🔍 جاري الفحص، انتظر...")
    for key in CALENDAR_IDS:
        try:
            cal_id = CALENDAR_IDS[key]
            month  = get_months(1)[0]
            async with aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
            ) as session:
                html = await fetch_calendar(session, cal_id, month)
            if html is None:
                await msg.answer(f"❌ <b>{NAMES[key]}</b>: فشل جلب الصفحة", parse_mode="HTML"); continue
            dates = parse_dates(html)
            total = sum(dates.values())
            if dates:
                lines = "\n".join(f"  • {d} — {s} مكان" for d, s in sorted(dates.items()))
                await msg.answer(f"✅ <b>{NAMES[key]}</b> — المجموع: <b>{total} مكان</b>\n{lines}", parse_mode="HTML")
            else:
                await msg.answer(f"📭 <b>{NAMES[key]}</b>: لا مواعيد", parse_mode="HTML")
        except Exception as e:
            await msg.answer(f"💥 <b>{NAMES[key]}</b>: {e}", parse_mode="HTML")


# ══════════════════════════════════════════
#  🔁  Loop المراقبة
# ══════════════════════════════════════════

CHECKERS = {
    "algiers":      check_algiers,
    "constantine":  check_constantine,
    "oran":         check_oran,
    "oran_vip":     check_oran_vip,
}

last_daily_day: int = -1


async def monitor_loop():
    global last_heartbeat, last_daily_day

    _load_stats()
    await asyncio.sleep(5)

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔄 <b>تم تشغيل البوت بنجاح!</b>\n\n"
                + "\n".join(f"📍 {NAMES[k]}: كل {interval_label(k)}" for k in CALENDAR_IDS)
                + f"\n\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"فشل رسالة البدء: {e}")

    while True:
        now    = asyncio.get_event_loop().time()
        active = [k for k, v in state.items() if v]

        # ── Heartbeat ──
        if heartbeat_interval > 0 and (now - last_heartbeat) >= heartbeat_interval:
            try:
                active_str = ", ".join(NAMES[k] for k in active) or "لا يوجد"
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "💓 <b>البوت يعمل بشكل طبيعي</b>\n\n"
                        f"المراكز النشطة: {active_str}\n"
                        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                    ),
                    parse_mode="HTML"
                )
                last_heartbeat = now
            except Exception as e:
                log.error(f"فشل Heartbeat: {e}")

        # ── تقرير يومي (8 صبح) ──
        today = datetime.now()
        if today.hour == 8 and today.day != last_daily_day:
            last_daily_day = today.day
            asyncio.create_task(_send_daily_report())

        if not active:
            log.info("💤 جميع المراكز متوقفة")
            await asyncio.sleep(60)
            continue

        log.info(f"🔍 فحص: {', '.join(active)}")

        for key in active:
            elapsed = now - last_checked[key]
            if elapsed < CHECK_INTERVALS[key]:
                log.info(f"⏳ {NAMES[key]}: متبقي {int(CHECK_INTERVALS[key]-elapsed)}ث")
                continue

            try:
                result = await CHECKERS[key]()
                last_checked[key] = asyncio.get_event_loop().time()

                # ── كشف فشل الاتصال ──
                if result is None:
                    consecutive_failures[key] += 1
                    log.warning(f"⚠️ {NAMES[key]}: فشل #{consecutive_failures[key]}")
                    if consecutive_failures[key] == FAILURE_ALERT_AFTER:
                        try:
                            await bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    f"🔴 <b>تحذير: فشل الاتصال بـ {NAMES[key]}</b>\n"
                                    f"فشل {FAILURE_ALERT_AFTER} مرات متتالية\n"
                                    f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                                ),
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            log.error(f"فشل إرسال تحذير الفشل: {e}")
                    continue

                # اتصال ناجح → نصفر عداد الفشل
                if consecutive_failures[key] > 0:
                    log.info(f"✅ {NAMES[key]}: عاد للعمل بعد {consecutive_failures[key]} فشل")
                    consecutive_failures[key] = 0

                dates = result
                total = sum(dates.values())
                prev  = last_total[key]
                log.info(f"📊 {NAMES[key]}: {total} مكان ({len(dates)} تاريخ) — prev={prev}")

                _update_stats(key, total)

                # ── إشعار النقصان ──
                drop_thr = DROP_THRESHOLDS[key]
                if drop_thr > 0 and prev is not None:
                    drop = prev - total
                    if drop >= drop_thr:
                        log.info(f"📉 {NAMES[key]}: نقص {drop} ({prev}→{total})")
                        if not is_quiet_time() and not is_paused():
                            await _send_drop_alert(key, dates, total, prev, drop)
                            _update_stats(key, total, "drop")

                if not is_quiet_time() and not is_paused():
                    if dates:
                        # ── أول فحص: أرسل إشعار عام ──
                        if prev is None:
                            log.info(f"🔔 {NAMES[key]}: أول فحص — إرسال إشعار عام")
                            await _send_alert(key, dates)
                            _update_stats(key, total, "alert")

                        # ── ارتفعت المواعيد ──
                        elif total > prev:
                            rise = total - prev
                            log.info(f"📈 {NAMES[key]}: ارتفع {rise} ({prev}→{total})")
                            await _send_rise_alert(key, dates, total, prev, rise)
                            _update_stats(key, total, "rise")

                        # ── نفس العدد أو نقص (تم التعامل معه بـ drop_alert) ──
                        else:
                            log.info(f"➖ {NAMES[key]}: لا تغيير يستدعي إشعاراً ({prev}→{total})")

                    else:
                        log.info(f"📭 {NAMES[key]}: لا مواعيد")

                elif is_quiet_time():
                    log.info(f"🌙 {NAMES[key]}: ساعة صامتة — تم تخطي الإشعار")
                elif is_paused():
                    log.info(f"⏸ {NAMES[key]}: موقوف مؤقتاً — تم تخطي الإشعار")

                # ── إشعار آخر مكان ──
                if 0 < total <= 2 and not is_quiet_time() and not is_paused():
                    log.info(f"🚨 {NAMES[key]}: آخر {total} مكان!")
                    await _send_last_seats_alert(key, dates, total)

                last_total[key] = total

            except Exception as e:
                log.error(f"خطأ في فحص {key}: {e}")

        await asyncio.sleep(60)


# ══════════════════════════════════════════
#  📣  دوال الإرسال
# ══════════════════════════════════════════

async def _send_to_group(key: str, text: str):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="📅 احجز الآن",
            url=f"https://appointment.mosaicvisa.com/calendar/{CALENDAR_IDS[key]}"
        )
    ]])
    try:
        log.info(f"🔔 إرسال إشعار → القروب {CHAT_ID} [{NAMES[key]}]")
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", reply_markup=kb)
        log.info(f"✅ تم إرسال الإشعار بنجاح [{NAMES[key]}]")
    except Exception as e:
        log.error(f"❌ فشل إرسال الإشعار [{NAMES[key]}]: {e}")


async def _send_alert(key: str, dates: dict[str, int]):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    await _send_to_group(key,
        f"🚨🚨🚨 <b>مواعيد متاحة!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n\n"
        f"📅 <b>التواريخ:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )


async def _send_drop_alert(key: str, dates: dict[str, int], total: int, prev: int, drop: int):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    await _send_to_group(key,
        f"📉📉 <b>نقصت المواعيد!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n"
        f"🔻 نقص: <b>{drop} مكان</b>  ({prev} ← <b>{total}</b>)\n\n"
        f"📅 <b>المتبقي:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>سارع بالحجز!</i>"
    )

async def _send_rise_alert(key: str, dates: dict[str, int], total: int, prev: int, rise: int):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    await _send_to_group(key,
        f"📈📈 <b>زادت المواعيد!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n"
        f"🔺 زاد: <b>{rise} مكان</b>  ({prev} ← <b>{total}</b>)\n\n"
        f"📅 <b>المتاح الآن:</b>\n{lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>فرصة للحجز!</i>"
    )

async def _send_last_seats_alert(key: str, dates: dict[str, int], total: int):
    lines = "\n".join(f"  • {d} — <b>{s} مكان</b>" for d, s in sorted(dates.items()))
    await _send_to_group(key,
        f"🆘🆘🆘 <b>آخر {total} مكان!</b>\n\n"
        f"📍 <b>{NAMES[key]}</b>\n\n"
        f"📅 {lines}\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚡ <i>احجز الآن قبل فوات الأوان!</i>"
    )

async def _send_daily_report():
    lines = []
    for key in CALENDAR_IDS:
        s = stats.get(key, {})
        icon = status_icon(key)
        lines.append(
            f"{icon} <b>{NAMES[key]}</b>\n"
            f"   فحوصات اليوم: {s.get('checks_done', 0)}\n"
            f"   إشعارات أُرسلت: {s.get('alerts_sent', 0)}\n"
            f"   أعلى رصيد: {s.get('peak_total', 0)} مكان\n"
            f"   آخر إشعار: {s.get('last_alert') or 'لا يوجد'}"
        )
    quiet_str = f"{quiet_start:02d}:00 → {quiet_end:02d}:00" if (quiet_start or quiet_end) else "معطلة"
    text = (
        f"📋 <b>التقرير اليومي — {datetime.now().strftime('%d/%m/%Y')}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(lines)
        + f"\n\n🌙 ساعات صامتة: {quiet_str}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        log.error(f"فشل التقرير اليومي: {e}")


# ══════════════════════════════════════════
#  🚀  نقطة الدخول
# ══════════════════════════════════════════

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start",           description="الرئيسية"),
        BotCommand(command="check",           description="فحص فوري"),
        BotCommand(command="stats",           description="الإحصائيات"),
        BotCommand(command="reset",           description="إعادة تعيين إحصائيات مركز"),
        BotCommand(command="daily",           description="تقرير يومي فوري"),
        BotCommand(command="intervals",       description="عرض التواقيت"),
        BotCommand(command="interval",        description="تغيير توقيت مركز"),
        BotCommand(command="drops",           description="عرض حدود النقصان"),
        BotCommand(command="drop",            description="تغيير حد النقصان"),
        BotCommand(command="quiet",           description="الساعات الصامتة"),
        BotCommand(command="pause",           description="إيقاف مؤقت للإشعارات"),
        BotCommand(command="heartbeat",       description="نبض التأكيد"),
        BotCommand(command="algiers_on",      description="تشغيل الجزائر"),
        BotCommand(command="algiers_off",     description="إيقاف الجزائر"),
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
