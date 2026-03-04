# -*- coding: utf-8 -*-
"""
Автоответчик писем: регистрация гарантии и обращения.
- Обращение (care): проверка по таблице гарантии → разный ответ и уведомление админу.
- Регистрация (warranty): если данные уже есть в таблице регистрации → ответ «спасибо за ещё одну регистрацию».
"""

import os
import re
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# Каталог с черновиками ответов
DATA_DIR = Path(__file__).resolve().parent / "data"
# Время последней проверки (с точностью до секунды), письма раньше не обрабатываем
LAST_CHECK_FILE = Path(__file__).resolve().parent / "last_check.txt"

# Google Sheets scope
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]


def get_sheet(spreadsheet_id: str):
    """
    Подключение к Google-таблице. spreadsheet_id — ID документа из URL:
    https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
    Используется первый лист. Таблицу нужно открыть для service account (email из creds.json).
    """
    creds = Credentials.from_service_account_file("creds.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    return sh.sheet1


def parse_key_value_body(text: str) -> dict:
    """Из тела письма извлекает пары вида «Ключ: значение» (в т.ч. с подчёркиванием)."""
    result = {}
    # Поддержка форматов: "Name: Иван", "Артикул_товара: 123", "дата_с_чека: 24.02.2026"
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Za-zА-Яа-яёЁ_\d]+)\s*:\s*(.+)$", line)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            if key and value:
                result[key] = value
    return result


def detect_type_and_extract(msg, mailbox: str) -> Tuple[Optional[str], dict]:
    """
    Определяет тип письма: 'registration' (регистрация) или 'care' (обращение).
    mailbox: 'care' | 'warranty'
    Возвращает (type, parsed_dict).
    """
    subject = ""
    body = ""
    if msg["Subject"]:
        subject = email.header.decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes):
            subject = subject.decode("utf-8", errors="replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            raw = part.get_payload(decode=True)
            if raw:
                body = raw.decode("utf-8", errors="replace")
            break
    if not body and msg.get_payload(decode=True):
        body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

    parsed = parse_key_value_body(body)

    # По ящику и теме/содержимому
    if mailbox == "warranty":
        if "Новый заказ" in subject or "Регистрация гарантии" in body or "Информация о покупателе" in body:
            # Нормализуем ключи под общий формат (в письме регистрации: Артикул, Номер_чека)
            if "Артикул" in parsed and "Номер_чека" not in parsed:
                pass  # уже есть
            return "registration", parsed
        return None, parsed

    if mailbox == "care":
        if "Заявка с сайта" in subject or "Request from" in subject or "Содержание заявки" in body or "Request details" in body or "Проблема" in parsed:
            # В обращениях бывает Артикул_товара, Номер_чека, дата_с_чека
            art = parsed.get("Артикул_товара") or parsed.get("Артикул")
            if art:
                parsed["_артикул"] = art
            return "care", parsed
        return None, parsed

    return None, parsed


def find_in_sheet(sheet, art: str, nomer_cheka: Optional[str] = None) -> bool:
    """Проверяет, есть ли запись в таблице по артикулу и при необходимости по номеру чека."""
    try:
        rows = sheet.get_all_records()
    except Exception:
        rows = []
    if not rows:
        # может быть заголовок в первой строке, данные со второй
        try:
            all_values = sheet.get_all_values()
            if len(all_values) < 2:
                return False
            headers = [str(h).strip().lower() for h in all_values[0]]
            for r in all_values[1:]:
                row_dict = dict(zip(headers, (r + [""] * len(headers))[:len(headers)]))
                if _row_matches(row_dict, art, nomer_cheka):
                    return True
            return False
        except Exception:
            return False
    for row in rows:
        row_lower = {str(k).strip().lower(): str(v).strip() if v else "" for k, v in row.items()}
        if _row_matches(row_lower, art, nomer_cheka):
            return True
    return False


def _row_matches(row: dict, art: str, nomer_cheka: Optional[str]) -> bool:
    art = (art or "").strip()
    nomer_cheka = (nomer_cheka or "").strip()
    art_keys = ["артикул", "артикул_товара"]
    num_keys = ["номер_чека", "номер чека", "номер_чека_и_дата"]
    row_art = ""
    row_num = ""
    for k, v in row.items():
        k = k.lower().replace(" ", "_")
        if k in art_keys or "артикул" in k:
            row_art = v
        if k in num_keys or "номер_чека" in k or "номер чека" in k:
            row_num = v
    if not art:
        return False
    if row_art.strip() != art:
        return False
    if nomer_cheka and row_num and row_num != nomer_cheka:
        return False
    return True


def get_client_email(parsed: dict) -> str:
    """Email клиента для ответа."""
    return (
        parsed.get("ma_email")
        or parsed.get("Email")
        or parsed.get("email")
        or ""
    ).strip()


def load_last_check_time() -> datetime:
    """Время последней проверки (UTC). Если файла нет — «сутки назад»."""
    if not LAST_CHECK_FILE.exists():
        return datetime.now(timezone.utc) - timedelta(days=1)
    try:
        s = LAST_CHECK_FILE.read_text(encoding="utf-8").strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=1)


def save_last_check_time(dt: datetime):
    """Сохранить время проверки (ожидается UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    LAST_CHECK_FILE.write_text(dt.isoformat(), encoding="utf-8")


def send_email(login: str, password: str, to: str, subject: str, body: str, reply_to_msg_id: Optional[str] = None):
    """Отправка письма через Yandex SMTP."""
    print(f"[SMTP] Отправка письма: from={login} to={to} subject={subject}")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = login
    msg["To"] = to
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"] = reply_to_msg_id
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP("smtp.yandex.ru", 587) as smtp:
            smtp.starttls()
            smtp.login(login, password)
            smtp.sendmail(login, [to], msg.as_string())
        print(f"[SMTP] Успешно отправлено на {to}")
    except Exception as e:
        print(f"[SMTP] Ошибка при отправке на {to}: {e}")
        raise


def read_templates():
    """Загружает шаблоны из data/."""
    return {
        "care_response_found": (DATA_DIR / "care_response_found.txt").read_text(encoding="utf-8"),
        "care_response_not_found": (DATA_DIR / "care_response_not_found.txt").read_text(encoding="utf-8"),
        "care_admin_found": (DATA_DIR / "care_admin_found.txt").read_text(encoding="utf-8"),
        "care_admin_not_found": (DATA_DIR / "care_admin_not_found.txt").read_text(encoding="utf-8"),
        "reg_response_first": (DATA_DIR / "reg_response_first.txt").read_text(encoding="utf-8"),
        "reg_response_repeat": (DATA_DIR / "reg_response_repeat.txt").read_text(encoding="utf-8"),
        "reg_admin_first": (DATA_DIR / "reg_admin_first.txt").read_text(encoding="utf-8"),
        "reg_admin_repeat": (DATA_DIR / "reg_admin_repeat.txt").read_text(encoding="utf-8"),
    }


def process_care_mail(msg, parsed: dict, templates: dict, sheet_warranty, care_login: str, care_password: str, admin_email: str):
    """Обработка письма-обращения: проверка в таблице гарантии, ответ клиенту и уведомление админу."""
    art = parsed.get("Артикул_товара") or parsed.get("Артикул") or parsed.get("_артикул")
    nomer = parsed.get("Номер_чека") or parsed.get("Номер_чека_и_дата") or ""
    client_email = get_client_email(parsed)
    if not client_email:
        print("[CARE] Пропуск письма: не найден email клиента в parsed =", parsed)
        return
    print(f"[CARE] Обработка обращения: email={client_email!r}, артикул={art!r}, номер_чека={nomer!r}")
    found = find_in_sheet(sheet_warranty, art or "", nomer or None)
    subject_reply = "Re: Ваше обращение [ukataka.ru]"
    if found:
        body_reply = templates["care_response_found"]
        admin_body = templates["care_admin_found"]
    else:
        body_reply = templates["care_response_not_found"]
        admin_body = templates["care_admin_not_found"]
    print(f"[CARE] Результат поиска в таблице гарантии: found={found}")
    send_email(care_login, care_password, client_email, subject_reply, body_reply, msg.get("Message-ID"))
    if admin_email:
        send_email(care_login, care_password, admin_email, "[Обращение] Данные в гарантии: " + ("найдены" if found else "не найдены"), admin_body)


def process_registration_mail(msg, parsed: dict, templates: dict, sheet_reg, warranty_login: str, warranty_password: str, admin_email: str):
    """Обработка письма-регистрации: если уже есть в таблице регистрации — ответ «ещё одна регистрация»; уведомление админу."""
    art = parsed.get("Артикул")
    nomer = parsed.get("Номер_чека")
    client_email = get_client_email(parsed)
    if not client_email:
        print("[REG] Пропуск письма: не найден email клиента в parsed =", parsed)
        return
    print(f"[REG] Обработка регистрации: email={client_email!r}, артикул={art!r}, номер_чека={nomer!r}")
    already = find_in_sheet(sheet_reg, art or "", nomer or None)
    subject_reply = "Re: Регистрация гарантии [ukataka.ru]"
    body_reply = templates["reg_response_repeat"] if already else templates["reg_response_first"]
    print(f"[REG] Результат поиска в таблице регистрации: already={already}")
    send_email(warranty_login, warranty_password, client_email, subject_reply, body_reply, msg.get("Message-ID"))
    if admin_email:
        admin_body = templates["reg_admin_repeat"] if already else templates["reg_admin_first"]
        send_email(warranty_login, warranty_password, admin_email, "[Регистрация] " + ("повторная" if already else "новая") + " [ukataka.ru]", admin_body)


def _message_datetime(msg) -> Optional[datetime]:
    """Дата/время письма по заголовку Date (UTC для сравнения)."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def fetch_and_process_mailbox(imap, mailbox_name: str, sheet_warranty, sheet_reg, templates: dict, config: dict, last_check: datetime):
    """Выборка писем из INBOX, полученных после last_check (UTC)."""
    print(f"[IMAP] Проверка ящика {mailbox_name!r}, письма после {last_check.isoformat()}")
    imap.select("INBOX")
    since_str = last_check.strftime("%d-%b-%Y")
    try:
        _, data = imap.search(None, "SINCE " + since_str)
    except Exception as e:
        print(f"[IMAP] Ошибка поиска SINCE: {e}")
        return
    if not data or not data[0]:
        print(f"[IMAP] Писем с SINCE {since_str} нет в ящике {mailbox_name!r}")
        return
    for uid in data[0].split():
        uid = uid.decode() if isinstance(uid, bytes) else uid
        _, msg_data = imap.fetch(uid, "(RFC822)")
        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            msg = email.message_from_bytes(part[1])
            msg_dt = _message_datetime(msg)
            if msg_dt is None:
                print(f"[IMAP] UID={uid}: нет даты в письме, пропуск.")
                continue
            if msg_dt <= last_check:
                print(f"[IMAP] UID={uid}: дата {msg_dt.isoformat()} <= last_check, пропуск.")
                continue
            subject = msg.get("Subject", "")
            print(f"[IMAP] Обработка UID={uid}, дата={msg_dt.isoformat()}, Subject={subject!r}")
            letter_type, parsed = detect_type_and_extract(msg, mailbox_name)
            print(f"[IMAP] Тип письма: {letter_type!r}, parsed_keys={list(parsed.keys())}")
            if not letter_type:
                print("[IMAP] Не удалось определить тип письма, пропуск.")
                continue
            if mailbox_name == "care" and letter_type == "care":
                process_care_mail(
                    msg, parsed, templates,
                    sheet_warranty,
                    config["care_login"], config["care_password"],
                    config["admin_email"],
                )
            elif mailbox_name == "warranty" and letter_type == "registration":
                process_registration_mail(
                    msg, parsed, templates,
                    sheet_reg,
                    config["warranty_login"], config["warranty_password"],
                    config["admin_email"],
                )
            else:
                print(f"[IMAP] Тип {letter_type!r} не подходит для ящика {mailbox_name!r}, пропуск.")


def main():
    care_login = os.getenv("MAIL_USER_CARE", "").strip()
    care_password = os.getenv("MAIL_PASSWORD_CARE", "").strip()
    warranty_login = os.getenv("MAIL_USER_WARRANTY", "").strip()
    warranty_password = os.getenv("MAIL_PASSWORD_WARRANTY", "").strip()
    table_warranty_id = os.getenv("TABLE_WARRANTY", "").strip()
    table_reg_id = os.getenv("TABLE_REG", "").strip()
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()

    if not table_warranty_id or not table_reg_id:
        print("Укажите TABLE_WARRANTY и TABLE_REG в .env (ID таблиц Google Sheets).")
        return
    if not care_login or not care_password:
        print("Укажите MAIL_USER_CARE и MAIL_PASSWORD_CARE в .env.")
        return
    if not warranty_login or not warranty_password:
        print("Укажите MAIL_USER_WARRANTY и MAIL_PASSWORD_WARRANTY в .env.")
        return

    templates = read_templates()
    sheet_warranty = get_sheet(table_warranty_id)
    sheet_reg = get_sheet(table_reg_id)

    config = {
        "care_login": care_login,
        "care_password": care_password,
        "warranty_login": warranty_login,
        "warranty_password": warranty_password,
        "admin_email": admin_email,
    }

    last_check = load_last_check_time()
    print(f"[RUN] Время последней проверки: {last_check.isoformat()}")

    # Ящик обращений (care)
    try:
        imap_care = imaplib.IMAP4_SSL("imap.yandex.ru")
        imap_care.login(care_login, care_password)
        fetch_and_process_mailbox(imap_care, "care", sheet_warranty, sheet_reg, templates, config, last_check)
        imap_care.logout()
    except Exception as e:
        print("Ошибка при обработке ящика care:", e)

    # Ящик регистрации (warranty)
    try:
        imap_warranty = imaplib.IMAP4_SSL("imap.yandex.ru")
        imap_warranty.login(warranty_login, warranty_password)
        fetch_and_process_mailbox(imap_warranty, "warranty", sheet_warranty, sheet_reg, templates, config, last_check)
        imap_warranty.logout()
    except Exception as e:
        print("Ошибка при обработке ящика warranty:", e)

    now_utc = datetime.now(timezone.utc)
    save_last_check_time(now_utc)
    print(f"[RUN] Время проверки обновлено: {now_utc.isoformat()}")


if __name__ == "__main__":
    main()
