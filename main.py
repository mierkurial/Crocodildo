import asyncio
import logging
import time
import os
import html
import re
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import Command
from aiogram.enums import ParseMode

load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')
DB_FILE = 'dictionary.db'

router = Router()

db_conn: aiosqlite.Connection = None
active_timers = {}
cooldowns = {}
active_games_cache = {}


async def init_db():
    await db_conn.execute('PRAGMA journal_mode=WAL;')
    await db_conn.execute('PRAGMA synchronous=NORMAL;')

    await db_conn.execute('''CREATE TABLE IF NOT EXISTS users (
                        chat_id INTEGER,
                        user_id INTEGER,
                        username TEXT,
                        score INTEGER DEFAULT 0,
                        PRIMARY KEY (chat_id, user_id))''')
    await db_conn.execute('''CREATE TABLE IF NOT EXISTS words (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        word TEXT UNIQUE,
                        description TEXT,
                        used INTEGER DEFAULT 0)''')

    await db_conn.execute('CREATE INDEX IF NOT EXISTS idx_words_used ON words(used)')
    await db_conn.execute('CREATE INDEX IF NOT EXISTS idx_users_score ON users(chat_id, score DESC)')
    await db_conn.commit()


async def get_random_word(current_word: str = None):
    query = 'SELECT word, description FROM words WHERE used = 0'
    params = []

    if current_word:
        query += ' AND word != ?'
        params.append(current_word)

    query += ' ORDER BY RANDOM() LIMIT 1'

    async with db_conn.execute(query, params) as cursor:
        return await cursor.fetchone()


async def game_timeout(chat_id: int, bot: Bot):
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        return

    active_games_cache.pop(chat_id, None)
    active_timers.pop(chat_id, None)

    try:
        await bot.send_message(
            chat_id,
            "Прошло 5 минут без активности. Игра автоматически отменена.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.error(f"Timeout msg error: {e}")


def set_timer(chat_id: int, bot: Bot):
    cancel_timer(chat_id)
    task = asyncio.create_task(game_timeout(chat_id, bot))
    active_timers[chat_id] = task


def cancel_timer(chat_id: int):
    if chat_id in active_timers:
        active_timers[chat_id].cancel()
        del active_timers[chat_id]


async def clear_cooldown(chat_id: int, timestamp: float):
    await asyncio.sleep(5)
    if chat_id in cooldowns and cooldowns[chat_id]["time"] == timestamp:
        cooldowns.pop(chat_id, None)


def get_word_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Узнать слово", callback_data="get_word")],
                                                 [InlineKeyboardButton(text="Заменить", callback_data="change_word")]
                                                 ])


def get_new_host_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Хочу быть ведущим", callback_data="become_host")]
                         ])


@router.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "Команды:\n"
        "/play — Начать игру\n"
        "/stop — Остановить текущую игру\n"
        "/stats — Топ игроков"
    )
    await message.answer(text)


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    game_data = active_games_cache.get(chat_id)
    if not game_data:
        await message.answer("В этом чате сейчас нет активной игры.")
        return

    if user_id != game_data["host_id"]:
        await message.answer("Остановить игру может только ведущий.")
        return

    active_games_cache.pop(chat_id, None)
    cancel_timer(chat_id)

    await message.answer("Игра остановлена ведущим.")


@router.message(Command("play"))
async def cmd_play(message: Message):
    chat_id = message.chat.id

    if chat_id in active_games_cache:
        await message.answer("Игра уже идет в этом чате. Отгадайте текущее слово или введите /stop.")
        return

    word_data = await get_random_word()

    if not word_data:
        await message.answer("Слова в базе закончились.")
        return

    word, description = word_data
    host_id = message.from_user.id

    active_games_cache[chat_id] = {"word": word.lower(), "host_id": host_id, "description": description}
    set_timer(chat_id, message.bot)

    host_name = html.escape(message.from_user.full_name)
    host_link = f'<a href="tg://user?id={host_id}">{host_name}</a>'

    await message.answer(
        f"Игра началась.\nВедущий: {host_link}",
        reply_markup=get_word_kb(),
        parse_mode=ParseMode.HTML
    )


@router.callback_query(F.data == "get_word")
async def show_word(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    game_data = active_games_cache.get(chat_id)
    if not game_data:
        await callback.answer("В этом чате нет активной игры.", show_alert=True)
        return

    if user_id != game_data["host_id"]:
        await callback.answer("Ты не ведущий в этой игре.", show_alert=True)
        return

    word = game_data["word"]
    description = game_data.get("description")

    if description:
        description = re.sub(r'^[^А-ЯЁ]+', '', description)
        description = re.split(r'\s*(?:II|\|\|)', description)[0].strip()

    text = f"Слово: {word}\n\nОписание: {description if description else 'отсутствует.'}"

    if len(text) > 200:
        text = text[:197] + "..."

    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "change_word")
async def change_word(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    game_data = active_games_cache.get(chat_id)
    if not game_data:
        await callback.answer("В этом чате нет активной игры.", show_alert=True)
        return

    if user_id != game_data["host_id"]:
        await callback.answer("Ты не ведущий в этой игре.", show_alert=True)
        return

    current_word = game_data["word"]
    word_data = await get_random_word(current_word)

    if not word_data:
        await callback.answer("Других слов в базе больше нет.", show_alert=True)
        return

    new_word, description = word_data

    active_games_cache[chat_id] = {"word": new_word.lower(), "host_id": user_id, "description": description}
    set_timer(chat_id, callback.bot)

    if description:
        description = re.sub(r'^[^А-ЯЁ]+', '', description)
        description = re.split(r'\s*(?:II|\|\|)', description)[0].strip()

    text = f"Новое слово: {new_word}\n\nОписание: {description if description else 'отсутствует.'}"

    if len(text) > 200:
        text = text[:197] + "..."

    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "become_host")
async def become_host(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    if chat_id in cooldowns:
        cooldown_data = cooldowns[chat_id]
        time_passed = time.time() - cooldown_data["time"]

        if time_passed < 5:
            if user_id != cooldown_data["winner_id"]:
                time_left = round(5 - time_passed, 1)
                await callback.answer(f"У победителя есть фора. Жди еще {time_left} сек.", show_alert=True)
                return
        else:
            cooldowns.pop(chat_id, None)

    if chat_id in active_games_cache:
        await callback.answer("Игра уже идет.", show_alert=True)
        return

    word_data = await get_random_word()

    if not word_data:
        await callback.answer("Слова в базе закончились.", show_alert=True)
        return

    word, description = word_data

    active_games_cache[chat_id] = {"word": word.lower(), "host_id": user_id, "description": description}
    set_timer(chat_id, callback.bot)

    cooldowns.pop(chat_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)

    host_name = html.escape(callback.from_user.full_name)
    host_link = f'<a href="tg://user?id={user_id}">{host_name}</a>'

    await callback.message.answer(
        f"Игра началась.\nВедущий: {host_link}",
        reply_markup=get_word_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    chat_id = message.chat.id

    async with db_conn.execute(
            'SELECT username, score FROM users WHERE chat_id = ? ORDER BY score DESC LIMIT 10',
            (chat_id,)
    ) as cursor:
        top_players = await cursor.fetchall()

    if not top_players:
        await message.answer("Статистика пуста.")
        return

    stats_text = "<b>Топ 10 игроков этого чата:</b>\n\n"
    for i, (username, score) in enumerate(top_players, 1):
        escaped_username = html.escape(username) if username else 'Unknown'
        stats_text += f"{i}. {escaped_username} — {score} очков\n"

    await message.answer(stats_text, parse_mode=ParseMode.HTML)


@router.message(F.text)
async def check_word(message: Message):
    text = message.text.strip().lower()
    chat_id = message.chat.id
    game_data = active_games_cache.get(chat_id)

    if not game_data:
        return

    if message.from_user.id == game_data["host_id"]:
        return

    if text == game_data["word"]:
        active_games_cache.pop(chat_id, None)
        cancel_timer(chat_id)

        user_id = message.from_user.id
        username = message.from_user.full_name

        await db_conn.execute('''INSERT INTO users (chat_id, user_id, username, score) 
                            VALUES (?, ?, ?, 1) 
                            ON CONFLICT(chat_id, user_id) 
                            DO UPDATE SET score = score + 1, username = excluded.username''',
                              (chat_id, user_id, username))
        await db_conn.execute('UPDATE words SET used = 1 WHERE word = ?', (text,))
        await db_conn.commit()

        current_time = time.time()
        cooldowns[chat_id] = {"winner_id": user_id, "time": current_time}
        asyncio.create_task(clear_cooldown(chat_id, current_time))

        escaped_username = html.escape(username)
        user_link = f'<a href="tg://user?id={user_id}">{escaped_username}</a>'

        await message.answer(
            f"{user_link} отгадал(а) слово <b>{html.escape(text)}</b>.",
            reply_markup=get_new_host_kb(),
            parse_mode=ParseMode.HTML
        )


async def main():
    logging.basicConfig(level=logging.INFO)

    global db_conn
    db_conn = await aiosqlite.connect(DB_FILE)

    try:
        await init_db()

        bot = Bot(token=API_TOKEN)
        dp = Dispatcher()
        dp.include_router(router)

        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="play", description="Начать игру"),
            BotCommand(command="stop", description="Остановить игру"),
            BotCommand(command="stats", description="Показать статистику")
        ])

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db_conn.close()


if __name__ == "__main__":
    asyncio.run(main())