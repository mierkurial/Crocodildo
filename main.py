import asyncio
import logging
import time
import random
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

active_timers = {}
cooldowns = {}
active_games_cache = {}


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('PRAGMA synchronous=NORMAL;')

        await db.execute('''CREATE TABLE IF NOT EXISTS users (
                            chat_id INTEGER,
                            user_id INTEGER,
                            username TEXT,
                            score INTEGER DEFAULT 0,
                            PRIMARY KEY (chat_id, user_id))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS words (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            word TEXT UNIQUE,
                            description TEXT,
                            used INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS active_games (
                            chat_id INTEGER PRIMARY KEY,
                            host_id INTEGER,
                            current_word TEXT)''')

        await db.execute('CREATE INDEX IF NOT EXISTS idx_words_used ON words(used)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_score ON users(chat_id, score DESC)')

        await db.execute('DELETE FROM active_games')
        await db.commit()


async def get_random_word(db: aiosqlite.Connection, current_word: str = None):
    if current_word:
        cursor = await db.execute('SELECT COUNT(*) FROM words WHERE used = 0 AND word != ?', (current_word,))
    else:
        cursor = await db.execute('SELECT COUNT(*) FROM words WHERE used = 0')
    count = (await cursor.fetchone())[0]

    if count == 0:
        return None

    offset = random.randint(0, count - 1)

    if current_word:
        cursor = await db.execute('SELECT word, description FROM words WHERE used = 0 AND word != ? LIMIT 1 OFFSET ?',
                                  (current_word, offset))
    else:
        cursor = await db.execute('SELECT word, description FROM words WHERE used = 0 LIMIT 1 OFFSET ?', (offset,))

    return await cursor.fetchone()


async def game_timeout(chat_id: int, bot: Bot):
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        return

    active_games_cache.pop(chat_id, None)
    active_timers.pop(chat_id, None)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
        await db.commit()

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


def get_word_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Узнать слово", callback_data="get_word")],
        [InlineKeyboardButton(text="Заменить", callback_data="change_word")]
    ])


def get_new_host_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Хочу быть ведущим", callback_data="become_host")]
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

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
        await db.commit()

    await message.answer("Игра остановлена ведущим.")


@router.message(Command("play"))
async def cmd_play(message: Message):
    chat_id = message.chat.id

    if chat_id in active_games_cache:
        await message.answer("Игра уже идет в этом чате. Отгадайте текущее слово или введите /stop.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        word_data = await get_random_word(db)

        if not word_data:
            await message.answer("Слова в базе закончились.")
            return

        word, description = word_data
        host_id = message.from_user.id

        await db.execute(
            'INSERT OR REPLACE INTO active_games (chat_id, host_id, current_word) VALUES (?, ?, ?)',
            (chat_id, host_id, word)
        )
        await db.commit()

    active_games_cache[chat_id] = {"word": word.lower(), "host_id": host_id, "description": description}
    set_timer(chat_id, message.bot)

    host_link = f'<a href="tg://user?id={host_id}">{message.from_user.full_name}</a>'

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
        text = f"Слово: {word}\n\nОписание: {description}"
    else:
        text = f"Слово: {word}\n\nОписание отсутствует."

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

    async with aiosqlite.connect(DB_FILE) as db:
        word_data = await get_random_word(db, current_word)

        if not word_data:
            await callback.answer("Других слов в базе больше нет.", show_alert=True)
            return

        new_word, description = word_data

        await db.execute('UPDATE active_games SET current_word = ? WHERE chat_id = ?', (new_word, chat_id))
        await db.commit()

    active_games_cache[chat_id] = {"word": new_word.lower(), "host_id": user_id, "description": description}
    set_timer(chat_id, callback.bot)

    if description:
        text = f"Новое слово: {new_word}\n\nОписание: {description}"
    else:
        text = f"Новое слово: {new_word}\n\nОписание отсутствует."

    if len(text) > 200:
        text = text[:197] + "..."

    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "become_host")
async def become_host(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    host_name = callback.from_user.full_name

    if chat_id in cooldowns:
        cooldown_data = cooldowns[chat_id]
        time_passed = time.time() - cooldown_data["time"]

        if time_passed < 5:
            if user_id != cooldown_data["winner_id"]:
                time_left = round(5 - time_passed, 1)
                await callback.answer(f"У победителя есть фора. Жди еще {time_left} сек.", show_alert=True)
                return
        else:
            del cooldowns[chat_id]

    if chat_id in active_games_cache:
        await callback.answer("Игра уже идет.", show_alert=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        word_data = await get_random_word(db)

        if not word_data:
            await callback.answer("Слова в базе закончились.", show_alert=True)
            return

        word, description = word_data

        await db.execute(
            'INSERT INTO active_games (chat_id, host_id, current_word) VALUES (?, ?, ?)',
            (chat_id, user_id, word)
        )
        await db.commit()

    active_games_cache[chat_id] = {"word": word.lower(), "host_id": user_id, "description": description}
    set_timer(chat_id, callback.bot)

    if chat_id in cooldowns:
        del cooldowns[chat_id]

    await callback.message.edit_reply_markup(reply_markup=None)

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

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
                'SELECT username, score FROM users WHERE chat_id = ? ORDER BY score DESC LIMIT 10',
                (chat_id,)
        ) as cursor:
            top_players = await cursor.fetchall()

    if not top_players:
        await message.answer("Статистика пуста.")
        return

    stats_text = "<b>Топ 10 игроков этого чата:</b>\n\n"
    for i, (username, score) in enumerate(top_players, 1):
        stats_text += f"{i}. {username or 'Unknown'} — {score} очков\n"

    await message.answer(stats_text, parse_mode=ParseMode.HTML)


@router.message(F.text)
async def check_word(message: Message):
    text = message.text.strip().lower()
    if len(text.split()) > 1:
        return

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

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''INSERT INTO users (chat_id, user_id, username, score) 
                                VALUES (?, ?, ?, 1) 
                                ON CONFLICT(chat_id, user_id) 
                                DO UPDATE SET score = score + 1, username = excluded.username''',
                             (chat_id, user_id, username))
            await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
            await db.execute('UPDATE words SET used = 1 WHERE word = ?', (text,))
            await db.commit()

        cooldowns[chat_id] = {"winner_id": user_id, "time": time.time()}
        user_link = f'<a href="tg://user?id={user_id}">{username}</a>'

        await message.answer(
            f"{user_link} отгадал(а) слово <b>{text}</b>.",
            reply_markup=get_new_host_kb(),
            parse_mode=ParseMode.HTML
        )


async def main():
    logging.basicConfig(level=logging.INFO)
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


if __name__ == "__main__":
    asyncio.run(main())