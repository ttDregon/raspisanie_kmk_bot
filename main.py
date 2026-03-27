import asyncio
import logging
import urllib.parse
import aiohttp
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- Конфигурация ---
BOT_TOKEN = "8348901293:AAEnl8e8zyUteSewRK1NGNt2a5hvaVWOD6g"  # Твой токен вшит
SCHEDULE_URL = "http://80.91.179.229:81/cgi-bin/timetable.cgi"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# База данных пользователей в памяти (в идеале перевести на SQLite)
users_db = {}


# --- Машина состояний (FSM) ---
class Registration(StatesGroup):
    waiting_for_faculty = State()
    waiting_for_group = State()  # Ручной ввод названия группы


# --- Клавиатуры ---
def get_main_kb():
    """Главное меню переведено на украинский"""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Сьогодні"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="На тиждень")],
        [KeyboardButton(text="🔄 Змінити налаштування профілю")]
    ], resize_keyboard=True)


# --- Логика работы с сайтом ---
async def fetch_page(url: str, method='GET', data=None, params=None, headers=None):
    async with aiohttp.ClientSession() as session:
        try:
            if method == 'GET':
                async with session.get(url, params=params, ssl=False, headers=headers) as response:
                    html_bytes = await response.read()
            else:
                async with session.post(url, data=data, ssl=False, headers=headers) as response:
                    html_bytes = await response.read()
            try:
                return html_bytes.decode('utf-8'), response.status
            except UnicodeDecodeError:
                return html_bytes.decode('windows-1251'), response.status
        except Exception as e:
            logging.error(f"Ошибка сети: {e}")
            return None, None


async def parse_form_structure():
    html, status = await fetch_page(SCHEDULE_URL)
    if not html or status != 200:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    form = soup.find('form')
    if not form:
        return None

    form_method = form.get('method', 'GET').upper()
    form_action = form.get('action', SCHEDULE_URL)
    if not form_action.startswith('http'):
        form_action = SCHEDULE_URL

    selects = form.find_all('select')
    inputs = form.find_all('input')

    faculties = {}
    faculty_field_name = selects[0].get('name', 'faculty') if len(selects) > 0 else 'faculty'
    if len(selects) > 0:
        for opt in selects[0].find_all('option'):
            if opt.get('value') and opt.get('value') != "0" and "Оберіть" not in opt.text:
                faculties[opt.get('value')] = opt.text.strip()

    course_field_name = selects[1].get('name', 'course') if len(selects) > 1 else 'course'

    date_from_name, date_to_name, group_field_name = 'sdate', 'edate', 'group'
    for inp in inputs:
        name = inp.get('name', '').lower()
        if 'sdate' in name or 'from' in name:
            date_from_name = inp.get('name')
        elif 'edate' in name or 'to' in name:
            date_to_name = inp.get('name')
        elif inp.get('type', 'text').lower() == 'text' and (
                'grup' in name or name == 'group' or 'груп' in inp.get('placeholder', '').lower()):
            group_field_name = inp.get('name')

    hidden_data = {inp.get('name'): inp.get('value', '') for inp in inputs if
                   inp.get('type') == 'hidden' and inp.get('name')}
    btn = form.find(['button', 'input'], type='submit')
    if btn and btn.get('name'): hidden_data[btn.get('name')] = btn.get('value', '')

    return {
        'method': form_method, 'action': form_action,
        'faculty_field': faculty_field_name, 'course_field': course_field_name,
        'group_field': group_field_name, 'date_from_field': date_from_name,
        'date_to_field': date_to_name, 'faculties': faculties,
        'hidden_data': hidden_data
    }


async def fetch_and_parse_schedule(user_data, start_date: str, end_date: str):
    structure = await parse_form_structure()
    if not structure:
        return "❌ Помилка: сайт коледжу тимчасово недоступний."

    request_data = dict(structure['hidden_data'])
    request_data[structure['faculty_field']] = user_data['faculty_val']
    if structure['course_field']: request_data[structure['course_field']] = "0"
    if structure['group_field']: request_data[structure['group_field']] = user_data['group_name']
    if structure['date_from_field']: request_data[structure['date_from_field']] = start_date
    if structure['date_to_field']: request_data[structure['date_to_field']] = end_date

    if structure['method'] == 'GET':
        html, status = await fetch_page(structure['action'], method='GET', params=request_data)
    else:
        try:
            encoded_data = urllib.parse.urlencode(request_data, encoding='windows-1251')
        except:
            encoded_data = urllib.parse.urlencode(request_data, encoding='utf-8')
        html, status = await fetch_page(structure['action'], method='POST', data=encoded_data,
                                        headers={'Content-Type': 'application/x-www-form-urlencoded'})

    if not html:
        return "❌ Помилка при завантаженні розкладу."

    soup = BeautifulSoup(html, 'html.parser')

    result_text = f"🎓 <b>Розклад: {user_data['group_name']}</b>\n"
    result_text += f"🗓 Період: <i>{start_date} — {end_date}</i>\n"

    elements = soup.find_all(['h4', 'h3', 'h2', 'b', 'strong', 'div', 'tr'])
    current_day = None
    day_has_data = False
    found_any_class = False

    num_emojis = {"0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣",
                  "8": "8️⃣"}

    for el in elements:
        if el.name != 'tr':
            text = el.get_text(separator=" ", strip=True)
            if re.search(r'\d{2}\.\d{2}\.\d{4}', text) and len(text) < 40 and "з " not in text:
                current_day = text
                day_has_data = False
            continue

        cols = [td.get_text(separator=" ", strip=True) for td in el.find_all(['td', 'th'], recursive=False)]
        cols = [c for c in cols if c]
        if not cols:
            continue

        first_col = cols[0].replace('.', '').strip()

        if first_col.isdigit() and len(first_col) <= 2:
            if current_day and not day_has_data:
                day_parts = current_day.split(" ", 1)
                if len(day_parts) == 2:
                    pretty_day = f"📆 <b>{day_parts[0]}</b> <i>({day_parts[1].capitalize()})</i>"
                else:
                    pretty_day = f"📆 <b>{current_day}</b>"

                result_text += f"\n➖➖➖➖➖➖➖➖➖➖\n{pretty_day}\n➖➖➖➖➖➖➖➖➖➖\n"
                day_has_data = True

            pair = cols[0]
            time_str = cols[1] if len(cols) > 1 else ""
            if len(time_str) == 11 and " " in time_str:
                time_str = time_str.replace(" ", " - ")

            pair_emoji = num_emojis.get(pair, f"▫️ {pair}")

            if len(cols) > 2:
                subject = " ".join(cols[2:])
                subject = subject.replace("✔️", "").strip()
                subject = subject.replace("дист.", "💻 <b>Дист:</b>")

                result_text += f"\n{pair_emoji} <b>{time_str}</b>\n╰ {subject}\n"
            else:
                result_text += f"\n{pair_emoji} <b>{time_str}</b>\n╰ 🛏 <i>Вільний час</i>\n"

            found_any_class = True

    if not found_any_class:
        return f"📅 На період <b>{start_date} - {end_date}</b> пари для групи <b>{user_data['group_name']}</b> відсутні."

    if len(result_text) > 4000:
        result_text = result_text[:4000] + "\n\n<i>...(повідомлення обрізано)</i>"

    return result_text


# --- ФОНОВАЯ ЗАДАЧА: Проверка изменений ---
async def check_schedule_updates(bot_instance: Bot):
    await asyncio.sleep(10)
    logging.info("Фонова проверка расписания запущена.")
    last_known_schedules = {}

    while True:
        try:
            target_groups = {}
            group_subscribers = {}

            for uid, data in users_db.items():
                g_name = data.get('group_name')
                f_val = data.get('faculty_val')
                if g_name and f_val:
                    if g_name not in target_groups:
                        target_groups[g_name] = f_val
                        group_subscribers[g_name] = []
                    group_subscribers[g_name].append(uid)

            if target_groups:
                now = datetime.now()
                sdate = now.strftime("%d.%m.%Y")
                edate = (now + timedelta(days=1)).strftime("%d.%m.%Y")

                for g_name, f_val in target_groups.items():
                    fake_user_data = {'faculty_val': f_val, 'group_name': g_name}
                    new_schedule = await fetch_and_parse_schedule(fake_user_data, sdate, edate)

                    if "❌" in new_schedule or "відсутні" in new_schedule:
                        continue

                    if g_name in last_known_schedules:
                        if last_known_schedules[g_name] != new_schedule:
                            logging.info(f"Найдено изменение в расписании для группы {g_name}!")

                            for uid in group_subscribers[g_name]:
                                try:
                                    msg = f"🔔 <b>УВАГА! Зміни в розкладі!</b>\nСайт коледжу оновив дані.\n\n{new_schedule}"
                                    await bot_instance.send_message(uid, msg, parse_mode="HTML")
                                except Exception as e:
                                    logging.error(f"Не удалось отправить уведомление {uid}: {e}")

                    last_known_schedules[g_name] = new_schedule
                    await asyncio.sleep(2)

        except Exception as e:
            logging.error(f"Ошибка в фоновом проверщике: {e}")

        await asyncio.sleep(1800)


# --- Обработчики FSM ---
@dp.message(Command("start"))
@dp.message(F.text == "🔄 Змінити налаштування профілю")
async def cmd_start(message: types.Message, state: FSMContext):
    # Приветственное сообщение только для команды /start
    if message.text == "/start":
        await message.answer(
            "Привіт! 👋 Я твій помічник з розкладу Криворізького фахового медичного коледжу 🏥.\n\n"
            "Давай швидко налаштуємо твій профіль, щоб ти міг бачити розклад своєї групи в один клік!"
        )

    wait_msg = await message.answer("🔄 Підключаюсь до сайту коледжу...")
    structure = await parse_form_structure()

    if not structure:
        await wait_msg.edit_text("❌ Помилка з'єднання з сайтом. Спробуй пізніше.")
        return

    await state.update_data(faculties=structure['faculties'])

    builder = InlineKeyboardBuilder()
    for val, text in structure['faculties'].items():
        builder.button(text=text, callback_data=f"fac_{val}")
    builder.adjust(1)

    await wait_msg.edit_text("🎓 <b>Обери свій факультет:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.set_state(Registration.waiting_for_faculty)


@dp.callback_query(Registration.waiting_for_faculty, F.data.startswith("fac_"))
async def process_faculty(callback: types.CallbackQuery, state: FSMContext):
    faculty_val = callback.data.replace("fac_", "")
    await state.update_data(faculty_val=faculty_val)

    await callback.message.edit_text("📝 <b>Напиши точну назву своєї групи текстом</b>\n<i>(Наприклад: ЛС9-3-1)</i>:",
                                     parse_mode="HTML")
    await state.set_state(Registration.waiting_for_group)


@dp.message(Registration.waiting_for_group)
async def process_group_input(message: types.Message, state: FSMContext):
    group_name = message.text.replace(" ", "").strip()

    users_db[message.from_user.id] = {**(await state.get_data()), 'group_name': group_name}

    await message.answer(f"✅ Профіль налаштовано!\nГрупа: <b>{group_name}</b>\nВикористовуй меню внизу:",
                         reply_markup=get_main_kb(), parse_mode="HTML")
    await state.clear()


@dp.message(F.text.in_({"Сьогодні", "Завтра", "На тиждень"}))
async def process_schedule_request(message: types.Message):
    user_data = users_db.get(message.from_user.id)
    if not user_data:
        await message.answer("⚠️ Спочатку треба налаштувати профіль. Натисни /start")
        return

    wait_msg = await message.answer("🔄 Завантажую розклад...")

    now = datetime.now()
    if message.text == "Сьогодні":
        sdate, edate = now, now
    elif message.text == "Завтра":
        sdate = edate = now + timedelta(days=1)
    else:
        sdate, edate = now, now + timedelta(days=7)

    schedule = await fetch_and_parse_schedule(user_data, sdate.strftime("%d.%m.%Y"), edate.strftime("%d.%m.%Y"))
    await wait_msg.edit_text(schedule, parse_mode="HTML")


async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен...")

    asyncio.create_task(check_schedule_updates(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
