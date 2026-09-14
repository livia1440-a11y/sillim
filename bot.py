"""
전날부터 당일 오전 9시까지 사진을 업로드하지 않은
'등록된 참가자'를 자동으로 멘션하는 텔레그램 봇

필요 라이브러리:
    pip install "python-telegram-bot[job-queue]" python-dotenv

실행:
    python bot.py
"""

import logging
import sqlite3
from datetime import datetime, time, timedelta
from html import escape
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os

from telegram import Update
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────
# 0. 기본 설정
# ─────────────────────────────────────────────
load_dotenv()  # .env 파일에서 BOT_TOKEN을 읽어옴

BOT_TOKEN = os.environ["BOT_TOKEN"]          # .env 에 BOT_TOKEN=123456:ABC... 형태로 저장
KST = ZoneInfo("Asia/Seoul")
# DB_PATH 환경변수가 있으면 그 경로를 쓰고, 없으면 현재 폴더에 저장
# (Railway에서는 Volume을 마운트한 경로로 DB_PATH를 지정해야 재배포해도 데이터가 유지됨)
DB_PATH = os.environ.get("DB_PATH", "photo_bot.db")

# 매일 몇 시에 점검 메시지를 보낼지
CHECK_HOUR, CHECK_MINUTE = 7, 0

# 미션이 없는 요일 (Python weekday: 월=0, 화=1, 수=2, 목=3, 금=4, 토=5, 일=6)
SKIP_WEEKDAYS = {1, 3, 5}  # 화, 목, 토 — 이 요일 몫은 점검하지 않음

# 미업로드자 멘션 뒤에 붙일 멘트 (Railway Variables의 MENTION_MESSAGE로 실제 문구를 설정하세요.
#  코드에는 조직 특유의 표현을 남기지 않기 위해 중립적인 기본값만 둡니다.)
MENTION_MESSAGE = os.environ.get("MENTION_MESSAGE", "아직 사진을 안 올리신 분들이에요, 확인 부탁드려요!")

# 자정 리마인드: 몇 시에 보낼지 / 어떤 문구를 보낼지 / 어떤 요일 다음날 자정에 보낼지
REMINDER_HOUR, REMINDER_MINUTE = 0, 0
REMINDER_MESSAGE = os.environ.get("REMINDER_MESSAGE", "취침 전까지 사진 업로드 잊지 마세요!")
REMINDER_WEEKDAYS = {0, 2, 4}  # 월, 수, 금 — 이 요일에서 다음날로 넘어가는 자정에 리마인드

# 이 봇이 동작할 그룹만 허용 (쉼표로 구분된 chat_id 목록). 비워두면 모든 그룹에서 동작(기존과 동일).
_allowed_raw = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {int(x) for x in _allowed_raw.split(",") if x.strip()} if _allowed_raw else None

# 사용자별 명령 연타 방지 (초 단위 최소 간격)
COMMAND_COOLDOWN_SECONDS = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("허용된 chat_id 목록(ALLOWED_CHAT_IDS): %s", ALLOWED_CHAT_IDS)


# 허용된 그룹에서만 동작하게 만드는 필터 (ALLOWED_CHAT_IDS 미설정 시 전체 허용 = 기존 동작 유지)
class _AllowedChatFilter(filters.MessageFilter):
    def filter(self, message):
        if ALLOWED_CHAT_IDS is None:
            return True
        return message.chat_id in ALLOWED_CHAT_IDS


allowed_chat_filter = _AllowedChatFilter()

# 사용자별 명령 연타 방지용 최근 호출 시각 기록
_last_command_ts: dict[int, float] = {}


def _rate_limited(user_id: int) -> bool:
    """너무 짧은 간격으로 같은 사용자가 다시 요청하면 True(막음)를 반환."""
    now = monotonic()
    last = _last_command_ts.get(user_id, 0.0)
    if now - last < COMMAND_COOLDOWN_SECONDS:
        return True
    _last_command_ts[user_id] = now
    return False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """예외를 서버 로그에만 남기고, 사용자에게는 세부 내용을 노출하지 않음."""
    logger.error("처리 중 오류 발생", exc_info=context.error)


async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """허용되지 않은 그룹에 봇이 추가되면 자동으로 나감 (ALLOWED_CHAT_IDS 설정 시에만 동작)."""
    if ALLOWED_CHAT_IDS is None:
        return

    my_chat_member = update.my_chat_member
    if not my_chat_member:
        return

    new_status = my_chat_member.new_chat_member.status
    chat = update.effective_chat
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and chat.id not in ALLOWED_CHAT_IDS:
        try:
            await context.bot.leave_chat(chat.id)
            logger.info("허용되지 않은 그룹(%s)이라 자동으로 나갔습니다.", chat.id)
        except Exception:
            logger.exception("허용되지 않은 그룹에서 나가기 실패")


# ─────────────────────────────────────────────
# 1. 데이터베이스 (SQLite)
# ─────────────────────────────────────────────
def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            active      INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def db_execute(query: str, params: tuple = (), fetch: bool = False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result


# ─────────────────────────────────────────────
# 2. 참가자 등록 / 관리 명령어
# ─────────────────────────────────────────────
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """참가자가 그룹에서 /등록 을 입력하면 스스로 등록됨"""
    user = update.effective_user
    chat = update.effective_chat

    if _rate_limited(user.id):
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("이 명령어는 단체 채팅방에서만 사용할 수 있어요.")
        return

    db_execute(
        """
        INSERT INTO participants (chat_id, user_id, username, full_name, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET username=excluded.username,
                      full_name=excluded.full_name,
                      active=1
        """,
        (chat.id, user.id, user.username, user.full_name),
    )
    await update.message.reply_text(f"{user.full_name}님, 참가자로 등록되었습니다 ✅")


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/탈퇴 - 본인을 명단에서 제외"""
    user = update.effective_user
    chat = update.effective_chat

    if _rate_limited(user.id):
        return

    db_execute(
        "UPDATE participants SET active=0 WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    await update.message.reply_text(f"{user.full_name}님을 명단에서 제외했습니다.")


async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """관리자가 다른 사람을 대신 명단에서 제외 (그 사람 메시지에 답장하며 '삭제' 입력)"""
    if _rate_limited(update.effective_user.id):
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("관리자만 사용할 수 있는 명령어예요.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "명단에서 뺄 사람의 메시지에 답장(reply)하면서 '삭제'라고 입력해주세요."
        )
        return

    target = update.message.reply_to_message.from_user
    chat = update.effective_chat
    db_execute(
        "UPDATE participants SET active=0 WHERE chat_id=? AND user_id=?",
        (chat.id, target.id),
    )
    await update.message.reply_text(f"{target.full_name}님을 명단에서 제외했습니다.")


async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid, 채팅ID - 이 그룹의 chat_id를 알려줌 (ALLOWED_CHAT_IDS 설정용, 관리자 전용 아님 — 그룹 확인용이라 누구나 가능)"""
    chat = update.effective_chat
    await update.message.reply_text(
        f"이 그룹의 chat_id는 다음과 같습니다:\n`{chat.id}`\n\n"
        "이 값을 Railway의 ALLOWED_CHAT_IDS 환경변수에 넣으면 이 그룹에서만 봇이 동작하게 됩니다.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/참가자목록 - 관리자만 실행 가능"""
    if _rate_limited(update.effective_user.id):
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("관리자만 사용할 수 있는 명령어예요.")
        return

    rows = db_execute(
        "SELECT full_name, username FROM participants WHERE chat_id=? AND active=1",
        (update.effective_chat.id,),
        fetch=True,
    )
    if not rows:
        await update.message.reply_text("등록된 참가자가 없습니다.")
        return

    text = "\n".join(f"• {name} (@{uname})" if uname else f"• {name}" for name, uname in rows)
    admin_id = update.effective_user.id
    try:
        # 참가자 명단(개인정보)은 그룹에 공개하지 않고 관리자 개인 DM으로만 전송
        await context.bot.send_message(admin_id, f"현재 등록된 참가자 ({len(rows)}명)\n{text}")
        await update.message.reply_text("명단을 DM으로 보내드렸어요.")
    except Exception:
        await update.message.reply_text(
            "DM 전송에 실패했어요. 먼저 봇과 1:1 대화를 한 번 시작(DM에서 아무 메시지나 전송)한 뒤 다시 시도해주세요."
        )


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await context.bot.get_chat_member(
        update.effective_chat.id, update.effective_user.id
    )
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


# ─────────────────────────────────────────────
# 3. 사진 업로드 감지
# ─────────────────────────────────────────────
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    now = datetime.now(KST)

    db_execute(
        "INSERT INTO uploads (chat_id, user_id, uploaded_at) VALUES (?, ?, ?)",
        (chat.id, user.id, now.isoformat()),
    )


# ─────────────────────────────────────────────
# 4. 매일 오전 9시 점검 + 멘션
# ─────────────────────────────────────────────
async def _run_missing_check(context: ContextTypes.DEFAULT_TYPE, window_start, window_end, message: str) -> bool:
    """지정된 구간에 이미지를 안 올린 참가자를 찾아 멘션. 하나라도 보냈으면 True 반환."""
    chat_ids = db_execute(
        "SELECT DISTINCT chat_id FROM participants WHERE active=1", fetch=True
    )

    sent_any = False
    for (chat_id,) in chat_ids:
        participants = db_execute(
            "SELECT user_id, username, full_name FROM participants WHERE chat_id=? AND active=1",
            (chat_id,),
            fetch=True,
        )
        uploaded_ids = {
            row[0]
            for row in db_execute(
                """
                SELECT DISTINCT user_id FROM uploads
                WHERE chat_id=? AND uploaded_at >= ? AND uploaded_at < ?
                """,
                (chat_id, window_start.isoformat(), window_end.isoformat()),
                fetch=True,
            )
        }

        missing = [p for p in participants if p[0] not in uploaded_ids]
        if not missing:
            continue

        await _send_mentions(context, chat_id, missing, message)
        sent_any = True

    return sent_any


async def check_and_mention(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    미션 요일: 월/수/금/일 (취침 전까지 이미지 업로드)
    화/목/토는 미션이 없는 날이라 점검하지 않음

    매일 아침 점검은 '어제' 몫을 확인하는 것이므로,
    어제가 화/목/토(미션 없는 날)였다면 이번 점검은 건너뜀.
    어제가 월/수/금/일이었다면 '전날 오후 3시 ~ 오늘 오전 7시' 구간에
    사진(이미지)을 한 장도 올리지 않은 활성 참가자를 멘션한다.
    동영상은 인정하지 않음 (photo 핸들러만 업로드로 기록하므로 자동으로 제외됨)
    """
    now = datetime.now(KST)
    yesterday = (now - timedelta(days=1)).date()

    if yesterday.weekday() in SKIP_WEEKDAYS:
        return

    window_end = now.replace(hour=CHECK_HOUR, minute=CHECK_MINUTE, second=0, microsecond=0)
    window_start = datetime.combine(yesterday, time(15, 0), tzinfo=KST)

    await _run_missing_check(context, window_start, window_end, MENTION_MESSAGE)


async def midnight_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    월/수/금에서 다음날로 넘어가는 자정(00:00)에,
    그날 15시부터 지금(자정)까지 아직 이미지를 안 올린 참가자에게
    미리 리마인드 메시지를 보낸다. (최종 점검은 아침 7시에 별도로 나감)
    """
    now = datetime.now(KST)
    # run_daily가 00:00에 실행되므로, 방금 끝난 미션 날짜는 하루 전으로 계산
    mission_day = (now - timedelta(seconds=1)).date()

    if mission_day.weekday() not in REMINDER_WEEKDAYS:
        return

    window_start = datetime.combine(mission_day, time(15, 0), tzinfo=KST)
    window_end = now

    await _run_missing_check(context, window_start, window_end, REMINDER_MESSAGE)


async def _send_mentions(context, chat_id, missing_users, message: str) -> None:
    mentions = []
    for user_id, username, full_name in missing_users:
        name = escape(full_name or (f"@{username}" if username else str(user_id)))
        # username이 있어도 없어도 항상 동작하는 text_mention 방식 (HTML)
        mentions.append(f'<a href="tg://user?id={user_id}">{name}</a>')

    # 텔레그램 메시지 길이 제한(4096자) 대응: 넘치면 나눠 보냄
    chunk, chunks, length = [], [], 0
    for m in mentions:
        if length + len(m) + 2 > 3800:
            chunks.append(chunk)
            chunk, length = [], 0
        chunk.append(m)
        length += len(m) + 2
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        text = ", ".join(c) + "\n" + message
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


_WEEKDAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/현황 - 관리자가 즉시 테스트로 실행해볼 수 있는 명령어 (아침 최종 점검, 요일 무시하고 강제 실행)"""
    if _rate_limited(update.effective_user.id):
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("관리자만 사용할 수 있는 명령어예요.")
        return

    now = datetime.now(KST)
    yesterday = (now - timedelta(days=1)).date()
    window_end = now
    window_start = datetime.combine(yesterday, time(15, 0), tzinfo=KST)

    is_off_day = yesterday.weekday() in SKIP_WEEKDAYS
    sent = await _run_missing_check(context, window_start, window_end, MENTION_MESSAGE)

    note = f" (참고: 어제({_WEEKDAY_NAMES[yesterday.weekday()]})는 원래 미션이 없는 날이라 실제 자동 점검은 건너뜁니다. 지금은 테스트라 강제로 실행했어요.)" if is_off_day else ""
    result = "미업로드자가 있어 메시지를 보냈습니다." if sent else "미업로드자가 없습니다."
    await update.message.reply_text(f"점검을 완료했습니다. {result}{note}")


async def manual_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/리마인드 - 관리자가 자정 리마인드를 즉시 테스트로 실행해볼 수 있는 명령어 (요일 무시하고 강제 실행)"""
    if _rate_limited(update.effective_user.id):
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("관리자만 사용할 수 있는 명령어예요.")
        return

    now = datetime.now(KST)
    mission_day = now.date()
    window_start = datetime.combine(mission_day, time(15, 0), tzinfo=KST)
    window_end = now

    is_off_day = mission_day.weekday() not in REMINDER_WEEKDAYS
    sent = await _run_missing_check(context, window_start, window_end, REMINDER_MESSAGE)

    note = f" (참고: 오늘({_WEEKDAY_NAMES[mission_day.weekday()]})은 원래 리마인드가 없는 날이라 실제 자동 발송은 건너뜁니다. 지금은 테스트라 강제로 실행했어요.)" if is_off_day else ""
    result = "미업로드자가 있어 메시지를 보냈습니다." if sent else "미업로드자가 없습니다."
    await update.message.reply_text(f"리마인드를 완료했습니다. {result}{note}")


# ─────────────────────────────────────────────
# 5. 앱 구성 및 실행
# ─────────────────────────────────────────────
def main() -> None:
    init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 텔레그램 명령어(/command)는 영어/숫자만 허용되므로 영어 명령어를 기본으로 두고,
    # 참가자들이 실제로 편하게 쓸 수 있도록 슬래시 없는 한글 단어도 함께 인식하게 처리
    # (ALLOWED_CHAT_IDS가 설정되어 있으면 그 그룹들에서만 반응)
    app.add_handler(CommandHandler("register", register, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?등록$") & allowed_chat_filter, register))

    app.add_handler(CommandHandler("unregister", unregister, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?탈퇴$") & allowed_chat_filter, unregister))

    app.add_handler(CommandHandler("list", list_participants, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?참가자목록$") & allowed_chat_filter, list_participants))

    app.add_handler(CommandHandler("remove", admin_remove, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?삭제$") & allowed_chat_filter, admin_remove))

    app.add_handler(CommandHandler("check", manual_check, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?현황$") & allowed_chat_filter, manual_check))

    app.add_handler(CommandHandler("remind_check", manual_reminder, filters=allowed_chat_filter))
    app.add_handler(MessageHandler(filters.Regex(r"^/?리마인드$") & allowed_chat_filter, manual_reminder))

    app.add_handler(MessageHandler(filters.PHOTO & allowed_chat_filter, on_photo))

    # chat_id 확인용 (ALLOWED_CHAT_IDS 설정 전/후 모두 사용 가능하도록 허용 필터 없이 등록)
    app.add_handler(CommandHandler("chatid", show_chat_id))
    app.add_handler(MessageHandler(filters.Regex(r"^/?채팅ID$"), show_chat_id))

    # 허용되지 않은 그룹에 추가되면 자동으로 나감 (ALLOWED_CHAT_IDS 설정 시에만 동작)
    app.add_handler(ChatMemberHandler(on_bot_added, ChatMemberHandler.MY_CHAT_MEMBER))

    # 예외는 서버 로그에만 남기고 사용자에게 세부 내용을 노출하지 않음
    app.add_error_handler(on_error)

    # 매일 07:00(KST) 최종 점검 + 멘션
    app.job_queue.run_daily(
        check_and_mention,
        time=time(CHECK_HOUR, CHECK_MINUTE, tzinfo=KST),
    )

    # 매일 00:00(KST) 자정 리마인드 (월/수/금 다음날 자정에만 실제로 발송됨)
    app.job_queue.run_daily(
        midnight_reminder,
        time=time(REMINDER_HOUR, REMINDER_MINUTE, tzinfo=KST),
    )

    logger.info("봇을 시작합니다...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
