import pdfplumber
import re
import pymorphy3
import sqlite3

PDF_PATH = 'dick.pdf'
DB_FILE = 'dictionary.db'


def extract_words_and_descriptions():
    morph = pymorphy3.MorphAnalyzer()
    word_data = {}
    pattern = re.compile(r'^\s*([А-ЯЁ]{3,}\d*)\b(.*?)(?=^\s*[А-ЯЁ]{3,}\d*\b|\Z)', re.MULTILINE | re.DOTALL)

    print("Чтение PDF файла.")
    full_text = ""
    with pdfplumber.open(PDF_PATH) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if text:
                full_text += text + "\n"
            if i % 100 == 0 or i == total_pages:
                print(f"Обработано страниц: {i}/{total_pages}")

    print("Извлечение словарных статей.")
    for match in pattern.finditer(full_text):
        word_raw = match.group(1)
        desc_raw = match.group(2).strip().replace('\n', ' ')

        word_clean = re.sub(r'\d+', '', word_raw).lower()
        parsed = morph.parse(word_clean)[0]

        if 'NOUN' in parsed.tag and parsed.normal_form == word_clean:
            if word_clean not in word_data:
                if desc_raw:
                    word_data[word_clean] = desc_raw

    return word_data


def update_database(word_data):
    print("Запись в базу данных.")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS words (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        word TEXT UNIQUE,
                        description TEXT,
                        used INTEGER DEFAULT 0)''')

    data = [(w, d) for w, d in word_data.items()]
    cursor.executemany('INSERT OR IGNORE INTO words (word, description) VALUES (?, ?)', data)

    conn.commit()
    conn.close()
    print(f"Процесс завершен. Добавлено слов: {len(data)}")


if __name__ == '__main__':
    data = extract_words_and_descriptions()
    update_database(data)