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
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os

from telegram import Update
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    ApplicationBuilder,
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
DB_PATH = "photo_bot.db"

# 매일 몇 시에 점검 메시지를 보낼지
CHECK_HOUR, CHECK_MINUTE = 9, 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 1. 데이터베이스 (SQLite)
# ─────────────────────────────────────────────
def init_db() -> None:
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
    db_execute(
        "UPDATE participants SET active=0 WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    await update.message.reply_text(f"{user.full_name}님을 명단에서 제외했습니다.")


async def list_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/참가자목록 - 관리자만 실행 가능"""
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
    await update.message.reply_text(f"현재 등록된 참가자 ({len(rows)}명)\n{text}")


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
async def check_and_mention(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    '전날부터 오전 9시까지' = 어제 00:00 ~ 오늘 09:00 사이에
    사진을 한 장도 올리지 않은 활성 참가자를 멘션한다.
    (원하는 기준이 '어제 09:00 ~ 오늘 09:00' 라면 window_start 계산만 바꾸면 됨)
    """
    now = datetime.now(KST)
    window_end = now.replace(hour=CHECK_HOUR, minute=CHECK_MINUTE, second=0, microsecond=0)
    yesterday = (now - timedelta(days=1)).date()
    window_start = datetime.combine(yesterday, time(0, 0), tzinfo=KST)

    chat_ids = db_execute(
        "SELECT DISTINCT chat_id FROM participants WHERE active=1", fetch=True
    )

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

        await _send_mentions(context, chat_id, missing, window_start, window_end)


async def _send_mentions(context, chat_id, missing_users, window_start, window_end) -> None:
    header = (
        f"📸 {window_start.strftime('%m/%d')} ~ {window_end.strftime('%m/%d %H:%M')} 사진 미업로드 참가자\n"
    )
    mentions = []
    for user_id, username, full_name in missing_users:
        name = escape(full_name or (f"@{username}" if username else str(user_id)))
        # username이 있어도 없어도 항상 동작하는 text_mention 방식 (HTML)
        mentions.append(f'<a href="tg://user?id={user_id}">{name}</a>')

    # 텔레그램 메시지 길이 제한(4096자) 대응: 넘치면 나눠 보냄
    chunk, chunks, length = [], [], len(header)
    for m in mentions:
        if length + len(m) + 2 > 3800:
            chunks.append(chunk)
            chunk, length = [], len(header)
        chunk.append(m)
        length += len(m) + 2
    if chunk:
        chunks.append(chunk)

    for i, c in enumerate(chunks):
        text = header + ", ".join(c) if i == 0 else ", ".join(c)
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/현황 - 관리자가 즉시 테스트로 실행해볼 수 있는 명령어"""
    if not await _is_admin(update, context):
        await update.message.reply_text("관리자만 사용할 수 있는 명령어예요.")
        return
    await check_and_mention(context)
    await update.message.reply_text("점검을 완료했습니다.")


# ─────────────────────────────────────────────
# 5. 앱 구성 및 실행
# ─────────────────────────────────────────────
def main() -> None:
    init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 텔레그램 명령어(/command)는 영어/숫자만 허용되므로 영어 명령어를 기본으로 두고,
    # 참가자들이 실제로 편하게 쓸 수 있도록 슬래시 없는 한글 단어도 함께 인식하게 처리
    app.add_handler(CommandHandler("register", register))
    app.add_handler(MessageHandler(filters.Regex(r"^/?등록$"), register))

    app.add_handler(CommandHandler("unregister", unregister))
    app.add_handler(MessageHandler(filters.Regex(r"^/?탈퇴$"), unregister))

    app.add_handler(CommandHandler("list", list_participants))
    app.add_handler(MessageHandler(filters.Regex(r"^/?참가자목록$"), list_participants))

    app.add_handler(CommandHandler("check", manual_check))
    app.add_handler(MessageHandler(filters.Regex(r"^/?현황$"), manual_check))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    # 매일 09:00(KST)에 자동 실행
    app.job_queue.run_daily(
        check_and_mention,
        time=time(CHECK_HOUR, CHECK_MINUTE, tzinfo=KST),
    )

    logger.info("봇을 시작합니다...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
