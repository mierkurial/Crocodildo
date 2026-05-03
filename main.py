import asyncio
import logging
import time
import os
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


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
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
        await db.commit()


async def game_timeout(chat_id: int, bot: Bot):
    await asyncio.sleep(300)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
        await db.commit()

    try:
        await bot.send_message(
            chat_id,
            "Прошло 5 минут без активности. Игра автоматически отменена.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    if chat_id in active_timers:
        del active_timers[chat_id]


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
        [InlineKeyboardButton(text="Подсказка", callback_data="get_hint")],
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

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT host_id FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            game = await cursor.fetchone()
            if not game:
                await message.answer("В этом чате сейчас нет активной игры.")
                return

            host_id = game[0]
            if user_id != host_id:
                await message.answer("Остановить игру может только ведущий.")
                return

        await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
        await db.commit()

    cancel_timer(chat_id)
    await message.answer("Игра остановлена ведущим.")


@router.message(Command("play"))
async def cmd_play(message: Message):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT chat_id FROM active_games WHERE chat_id = ?', (message.chat.id,)) as cursor:
            if await cursor.fetchone():
                await message.answer("Игра уже идет в этом чате. Отгадайте текущее слово или введите /stop.")
                return

        async with db.execute('SELECT word FROM words WHERE used = 0 ORDER BY RANDOM() LIMIT 1') as cursor:
            word_data = await cursor.fetchone()

        if not word_data:
            await message.answer("Слова в базе закончились.")
            return

        word = word_data[0]
        host_id = message.from_user.id

        await db.execute(
            'INSERT OR REPLACE INTO active_games (chat_id, host_id, current_word) VALUES (?, ?, ?)',
            (message.chat.id, host_id, word)
        )
        await db.commit()

    set_timer(message.chat.id, message.bot)

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

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT host_id, current_word FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            game = await cursor.fetchone()

    if not game:
        await callback.answer("В этом чате нет активной игры.", show_alert=True)
        return

    host_id, current_word = game

    if user_id != host_id:
        await callback.answer("Ты не ведущий в этой игре.", show_alert=True)
        return

    await callback.answer(f"Слово: {current_word}", show_alert=True)


@router.callback_query(F.data == "get_hint")
async def show_hint(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT host_id, current_word FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            game = await cursor.fetchone()

        if not game:
            await callback.answer("В этом чате нет активной игры.", show_alert=True)
            return

        host_id, current_word = game

        if user_id != host_id:
            await callback.answer("Ты не ведущий в этой игре.", show_alert=True)
            return

        async with db.execute('SELECT description FROM words WHERE word = ?', (current_word,)) as cursor:
            word_data = await cursor.fetchone()

        if not word_data or not word_data[0]:
            await callback.answer("Для этого слова нет описания.", show_alert=True)
            return

        description = word_data[0]

    await callback.answer(f"Подсказка:\n{description}", show_alert=True)


@router.callback_query(F.data == "change_word")
async def change_word(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT host_id, current_word FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            game = await cursor.fetchone()

        if not game:
            await callback.answer("В этом чате нет активной игры.", show_alert=True)
            return

        host_id, current_word = game

        if user_id != host_id:
            await callback.answer("Ты не ведущий в этой игре.", show_alert=True)
            return

        async with db.execute(
                'SELECT word FROM words WHERE used = 0 AND word != ? ORDER BY RANDOM() LIMIT 1',
                (current_word,)
        ) as cursor:
            word_data = await cursor.fetchone()

        if not word_data:
            await callback.answer("Других слов в базе больше нет.", show_alert=True)
            return

        new_word = word_data[0]

        await db.execute('UPDATE active_games SET current_word = ? WHERE chat_id = ?', (new_word, chat_id))
        await db.commit()

    set_timer(chat_id, callback.bot)

    await callback.answer(f"Слово заменено.\nНовое слово: {new_word}", show_alert=True)


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

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT chat_id FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            if await cursor.fetchone():
                await callback.answer("Игра уже идет.", show_alert=True)
                return

        async with db.execute('SELECT word FROM words WHERE used = 0 ORDER BY RANDOM() LIMIT 1') as cursor:
            word_data = await cursor.fetchone()

        if not word_data:
            await callback.answer("Слова в базе закончились.", show_alert=True)
            return

        word = word_data[0]

        await db.execute(
            'INSERT INTO active_games (chat_id, host_id, current_word) VALUES (?, ?, ?)',
            (chat_id, user_id, word)
        )
        await db.commit()

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
    chat_id = message.chat.id
    text = message.text.lower().strip()

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT current_word, host_id FROM active_games WHERE chat_id = ?', (chat_id,)) as cursor:
            game = await cursor.fetchone()

        if not game:
            return

        current_word, host_id = game

        if message.from_user.id == host_id:
            return

        if text == current_word.lower():
            cancel_timer(chat_id)

            user_id = message.from_user.id
            username = message.from_user.full_name

            await db.execute('''INSERT INTO users (chat_id, user_id, username, score) 
                                VALUES (?, ?, ?, 1) 
                                ON CONFLICT(chat_id, user_id) 
                                DO UPDATE SET score = score + 1, username = excluded.username''',
                             (chat_id, user_id, username))

            await db.execute('DELETE FROM active_games WHERE chat_id = ?', (chat_id,))
            await db.execute('UPDATE words SET used = 1 WHERE word = ?', (current_word,))
            await db.commit()

            cooldowns[chat_id] = {"winner_id": user_id, "time": time.time()}

            user_link = f'<a href="tg://user?id={user_id}">{username}</a>'

            await message.answer(
                f"{user_link} отгадал(а) слово <b>{current_word}</b>.",
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