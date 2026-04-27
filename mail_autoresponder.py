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
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# Каталог с черновиками ответов
DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_DIR = Path(__file__).resolve().parent

# Google Sheets scope
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
MAX_REPLY_AGE_DAYS = 2


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


def strip_html(text: str) -> str:
    """Удаляет HTML-теги из строки (при получении из писем часто прилетают <br>, <span> и т.п.)."""
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def normalize_email(addr: str) -> str:
    """Готовит email для отправки и поиска: убирает HTML-теги и все пробелы."""
    s = str(addr or "")
    # s = re.sub(r"<[^>]+>", "", s)   # убрать <br>, <span> и любые теги
    s = re.sub(r"\s+", "", s)         # убрать все пробелы, переносы, табы
    s = s.replace('br','')
    print('normalize', str(s))
    return s.strip()


def parse_key_value_body(text: str) -> dict:
    """Из тела письма извлекает пары вида «Ключ: значение» (в т.ч. с подчёркиванием)."""
    result = {}
    # Поддержка форматов: "Name: Иван", "Артикул_товара: 123", "дата_с_чека: 24.02.2026"
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Za-zА-Яа-яёЁ_\d]+)\s*:\s*(.+)$", line)
        if m:
            key = m.group(1).strip()
            value = strip_html(m.group(2))
            if key and value:
                result[key] = value
    return result


def html_to_lines(html: str) -> str:
    """Конвертирует HTML в построчный plain-text: <br> -> \n, теги убираются."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # убираем множественные пробелы/пустые строки
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(lines)


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

    html_body = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain":
            raw = part.get_payload(decode=True)
            if raw:
                body = raw.decode("utf-8", errors="replace")
            break
        if ct == "text/html" and not html_body:
            raw = part.get_payload(decode=True)
            if raw:
                html_body = raw.decode("utf-8", errors="replace")

    # Если plain-text части нет — используем HTML-часть с конвертацией
    if not body and html_body:
        body = html_to_lines(html_body)
        print(f"[PARSE] HTML сконвертирован в plain, первые 200 симв.: {body[:200]!r}")
    elif not body:
        raw = msg.get_payload(decode=True)
        if raw:
            body = raw.decode("utf-8", errors="replace")
    print(f"[PARSE] Итоговый body первые 200 симв.: {body[:200]!r}")

    parsed = parse_key_value_body(body)
    print(f"[PARSE] Parsed keys: {list(parsed.keys())}")
    print(f"[PARSE] Name={parsed.get('Name')!r}, Артикул={parsed.get('Артикул')!r}, Номер_чека={parsed.get('Номер_чека')!r}, Дата_чека_с_ВБ={parsed.get('Дата_чека_с_ВБ')!r}")

    # По ящику и теме/содержимому
    if mailbox == "warranty":
        if "Новый заказ" in subject or "Регистрация гарантии" in body or "Информация о покупателе" in body or "Содержание заявки" in body:
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


def find_in_sheet(
    sheet,
    art: str,
    nomer_cheka: Optional[str] = None,
    require_check_if_provided: bool = False,
) -> bool:
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
                if _row_matches(
                    row_dict,
                    art,
                    nomer_cheka,
                    require_check_if_provided=require_check_if_provided,
                ):
                    return True
            return False
        except Exception:
            return False
    for row in rows:
        row_lower = {str(k).strip().lower(): str(v).strip() if v else "" for k, v in row.items()}
        if _row_matches(
            row_lower,
            art,
            nomer_cheka,
            require_check_if_provided=require_check_if_provided,
        ):
            return True
    return False


def _row_matches(
    row: dict,
    art: str,
    nomer_cheka: Optional[str],
    require_check_if_provided: bool = False,
) -> bool:
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
    if nomer_cheka:
        if require_check_if_provided and not row_num:
            return False
        if row_num and row_num != nomer_cheka:
            return False
    return True


def get_client_email(parsed: dict) -> str:
    """Email клиента для ответа и поиска: без тегов и пробелов."""
    raw = (
        parsed.get("ma_email")
        or parsed.get("Email")
        or parsed.get("email")
        or ""
    )
    return normalize_email(raw)


def get_client_name(parsed: dict) -> str:
    raw = (
        parsed.get("Name")
        or parsed.get("name")
        or parsed.get("ma_name")
        or "клиент"
    )
    return strip_html(str(raw)).strip() or "клиент"


def get_purchase_date(parsed: dict) -> str:
    return (
        parsed.get("Дата_чека_с_ВБ")
        or parsed.get("Дата_покупки")
        or parsed.get("дата_с_чека")
        or parsed.get("Дата")
        or "даты в чеке"
    )


def render_template(template_text: str, context: dict) -> str:
    print(f"[TPL] Контекст подстановки: {context}")
    print(f"[TPL] Шаблон (первые 80 симв.): {template_text[:80]!r}")
    try:
        result = template_text.format(**context)
        print(f"[TPL] Результат (первые 80 симв.): {result[:80]!r}")
        return result
    except Exception as e:
        print(f"[TPL] ОШИБКА подстановки: {e}")
        return template_text


def is_message_recent_enough(msg, max_age_days: int = MAX_REPLY_AGE_DAYS) -> bool:
    """
    Отвечаем только на письма не старше max_age_days.
    Если Date отсутствует/непарсится, письмо пропускаем.
    """
    raw_date = msg.get("Date")
    if not raw_date:
        print("[IMAP] Пропуск: у письма нет заголовка Date.")
        return False
    try:
        msg_dt = parsedate_to_datetime(raw_date)
        if msg_dt.tzinfo is None:
            # Если TZ не указан, считаем письмо локальным временем сервера.
            msg_dt = msg_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        now_dt = datetime.now(msg_dt.tzinfo)
        age = now_dt - msg_dt
        if age > timedelta(days=max_age_days):
            print(f"[IMAP] Пропуск: письмо старше {max_age_days} дней (date={msg_dt.isoformat()}).")
            return False
        return True
    except Exception as e:
        print(f"[IMAP] Пропуск: не удалось распарсить Date ({raw_date!r}): {e}")
        return False


def is_incoming_candidate(msg, config: dict) -> bool:
    """
    Отсеиваем очевидно исходящие/служебные письма в общей папке «все письма»,
    чтобы бот не обрабатывал собственные ответы.
    """
    from_hdr = (msg.get("From") or "").lower()
    subj = (msg.get("Subject") or "").lower()
    own_accounts = [
        (config.get("care_login") or "").lower(),
        (config.get("warranty_login") or "").lower(),
    ]
    if any(acc and acc in from_hdr for acc in own_accounts):
        print("[IMAP] Пропуск: письмо отправлено с одного из наших ящиков.")
        return False
    # Страховка от авто-петель на ответы.
    if subj.startswith("re:") and ("регистрация гарантии" in subj or "ваше обращение" in subj):
        print("[IMAP] Пропуск: похоже на ответ на наше авто-письмо.")
        return False
    return True


def _sanitize_folder_name(folder_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", folder_name or "INBOX").strip("_")
    return safe.lower() or "inbox"


def _get_last_uid_file(mailbox_name: str, folder_name: str) -> Path:
    folder_key = _sanitize_folder_name(folder_name)
    return STATE_DIR / f"last_uid_{mailbox_name}_{folder_key}.txt"


def load_last_uid(mailbox_name: str, folder_name: str) -> int:
    """
    Последний обработанный UID для ящика.
    Если файла нет или сломан — считаем 0 (обрабатываем все письма).
    """
    path = _get_last_uid_file(mailbox_name, folder_name)
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def save_last_uid(mailbox_name: str, folder_name: str, uid: int):
    path = _get_last_uid_file(mailbox_name, folder_name)
    path.write_text(str(uid), encoding="utf-8")


def send_email(login: str, password: str, to: str, subject: str, body: str, reply_to_msg_id: Optional[str] = None):
    """Отправка письма через Yandex SMTP."""
    login = normalize_email(login)
    to = normalize_email(to)
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
    client_name = get_client_name(parsed)
    purchase_date = get_purchase_date(parsed)
    print(f"[CARE] Обработка обращения: name={client_name!r}, email={client_email!r}, артикул={art!r}, номер_чека={nomer!r}")
    found = find_in_sheet(sheet_warranty, art or "", nomer or None)
    subject_reply = "Re: Ваше обращение [ukataka.ru]"
    if found:
        body_reply = templates["care_response_found"]
        admin_body = templates["care_admin_found"]
    else:
        body_reply = templates["care_response_not_found"]
        admin_body = templates["care_admin_not_found"]
    print(f"[CARE] Результат поиска в таблице гарантии: found={found}")
    context = {
        "name": client_name,
        "article": art or "",
        "check_number": nomer or "",
        "purchase_date": purchase_date,
    }
    send_email(
        care_login,
        care_password,
        client_email,
        subject_reply,
        render_template(body_reply, context),
        msg.get("Message-ID"),
    )
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
    client_name = get_client_name(parsed)
    purchase_date = get_purchase_date(parsed)
    print(f"[REG] Обработка регистрации: name={client_name!r}, email={client_email!r}, артикул={art!r}, номер_чека={nomer!r}, дата={purchase_date!r}")
    already = find_in_sheet(sheet_reg, art or "", nomer or None, require_check_if_provided=True)
    subject_reply = "Re: Регистрация гарантии [ukataka.ru]"
    body_reply = templates["reg_response_repeat"] if already else templates["reg_response_first"]
    print(f"[REG] Результат поиска в таблице регистрации: already={already}")
    context = {
        "name": client_name,
        "article": art or "",
        "check_number": nomer or "",
        "purchase_date": purchase_date,
    }
    send_email(
        warranty_login,
        warranty_password,
        client_email,
        subject_reply,
        render_template(body_reply, context),
        msg.get("Message-ID"),
    )
    if admin_email:
        admin_body = templates["reg_admin_repeat"] if already else templates["reg_admin_first"]
        send_email(warranty_login, warranty_password, admin_email, "[Регистрация] " + ("повторная" if already else "новая") + " [ukataka.ru]", admin_body)


def fetch_and_process_mailbox(imap, mailbox_name: str, folder_name: str, sheet_warranty, sheet_reg, templates: dict, config: dict):
    """
    Выборка писем из INBOX, UID которых больше последнего обработанного.
    Это устойчиво к расхождению часов (ориентируемся только на UID).
    """
    print(f"[IMAP] Проверка ящика {mailbox_name!r}, папка {folder_name!r}")
    try:
        status, _ = imap.select(folder_name)
    except Exception as e:
        print(f"[IMAP] Ошибка select для папки {folder_name!r} в ящике {mailbox_name!r}: {e}")
        return
    if status != "OK":
        print(f"[IMAP] Не удалось открыть папку {folder_name!r} в ящике {mailbox_name!r}")
        return
    status, data = imap.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        print(f"[IMAP] В ящике {mailbox_name!r} нет писем.")
        return

    last_uid = load_last_uid(mailbox_name, folder_name)
    print(f"[IMAP] Последний обработанный UID для {mailbox_name!r}/{folder_name!r}: {last_uid}")

    uids_bytes = data[0].split()
    uids_int = []
    for u in uids_bytes:
        try:
            uids_int.append(int(u))
        except ValueError:
            continue
    uids_int.sort()

    to_process = [u for u in uids_int if u > last_uid]
    if not to_process:
        print(f"[IMAP] Новых писем (UID > {last_uid}) нет в ящике {mailbox_name!r}.")
        return

    max_processed = last_uid
    try:
        for uid_int in to_process:
            uid = str(uid_int)
            status, msg_data = imap.fetch(uid, "(RFC822)")
            if status != "OK":
                print(f"[IMAP] UID={uid}: ошибка fetch: {status}")
                max_processed = max(max_processed, uid_int)
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                subject = msg.get("Subject", "")
                print(f"[IMAP] Обработка UID={uid}, Subject={subject!r}")
                try:
                    if not is_incoming_candidate(msg, config):
                        break
                    if not is_message_recent_enough(msg):
                        break
                    letter_type, parsed = detect_type_and_extract(msg, mailbox_name)
                    print(f"[IMAP] Тип письма: {letter_type!r}, parsed_keys={list(parsed.keys())}")
                    if not letter_type:
                        print("[IMAP] Не удалось определить тип письма, пропуск.")
                        break
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
                except Exception as e:
                    print(f"[IMAP] Ошибка обработки UID={uid} в ящике {mailbox_name!r}: {e}")
                finally:
                    max_processed = max(max_processed, uid_int)
                break
    finally:
        if max_processed > last_uid:
            save_last_uid(mailbox_name, folder_name, max_processed)
            print(f"[IMAP] Для {mailbox_name!r}/{folder_name!r} сохранён последний UID: {max_processed}")


def get_target_folders(imap) -> list:
    """
    Возвращает папки для обработки:
    - предпочитаем одну общую папку «все письма» (если есть),
    - иначе fallback: INBOX + spam/junk/спам.
    """
    fallback_folders = ["INBOX"]
    all_mail_candidates = []
    try:
        status, data = imap.list()
        if status != "OK" or not data:
            return fallback_folders
        for row in data:
            line = row.decode(errors="replace")
            # LIST line format: (<flags>) "<delimiter>" "<mailbox>"  (or mailbox without quotes)
            # Надёжно достаём mailbox из конца строки.
            folder_name = ""
            m = re.match(r'.*\)\s+"[^"]*"\s+"(.*)"\s*$', line)
            if m:
                folder_name = m.group(1)
            else:
                m2 = re.match(r'.*\)\s+"[^"]*"\s+(.+)\s*$', line)
                if m2:
                    folder_name = m2.group(1).strip().strip('"')
                else:
                    parts = line.split()
                    folder_name = parts[-1].strip('"') if parts else ""
            low = folder_name.lower()
            if (
                "all" in low and "mail" in low
            ) or ("все" in low and "пись" in low):
                all_mail_candidates.append(folder_name)
            if (
                "spam" in low
                or "junk" in low
                or "спам" in low
            ):
                fallback_folders.append(folder_name)
    except Exception as e:
        print(f"[IMAP] Не удалось получить список папок: {e}")
    # Приоритет — одна общая папка.
    if all_mail_candidates:
        print(f"[IMAP] Будет использоваться общая папка: {all_mail_candidates[0]!r}")
        return [all_mail_candidates[0]]
    # Иначе fallback-режим.
    print("[IMAP] Общая папка 'all mail' не найдена, используем INBOX + spam/junk.")
    result = []
    seen = set()
    for f in fallback_folders:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


def run_iteration():
    care_login = normalize_email(os.getenv("MAIL_USER_CARE", ""))
    care_password = os.getenv("MAIL_PASSWORD_CARE", "").strip()
    warranty_login = normalize_email(os.getenv("MAIL_USER_WARRANTY", ""))
    warranty_password = os.getenv("MAIL_PASSWORD_WARRANTY", "").strip()
    table_warranty_id = os.getenv("TABLE_WARRANTY", "").strip()
    table_reg_id = os.getenv("TABLE_REG", "").strip()
    admin_email = normalize_email(os.getenv("ADMIN_EMAIL", ""))

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

    # Ящик обращений (care)
    try:
        imap_care = imaplib.IMAP4_SSL("imap.yandex.ru")
        imap_care.login(care_login, care_password)
        for folder in get_target_folders(imap_care):
            fetch_and_process_mailbox(imap_care, "care", folder, sheet_warranty, sheet_reg, templates, config)
        imap_care.logout()
    except Exception as e:
        print("Ошибка при обработке ящика care:", e)

    # Ящик регистрации (warranty)
    try:
        imap_warranty = imaplib.IMAP4_SSL("imap.yandex.ru")
        imap_warranty.login(warranty_login, warranty_password)
        for folder in get_target_folders(imap_warranty):
            fetch_and_process_mailbox(imap_warranty, "warranty", folder, sheet_warranty, sheet_reg, templates, config)
        imap_warranty.logout()
    except Exception as e:
        print("Ошибка при обработке ящика warranty:", e)


def main():
    interval_seconds = 120
    print("Автоответчик запущен. Основной цикл в Python, проверка почты каждые 2 минуты.")
    while True:
        start = datetime.now()
        print(f"[MAIN] Запуск проверки в {start.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        try:
            run_iteration()
        except Exception as e:
            print(f"[MAIN] Необработанная ошибка во время цикла: {e}")
        next_time = datetime.now() + timedelta(seconds=interval_seconds)
        print(f"[MAIN] Следующий запуск в {next_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("[MAIN] Получен KeyboardInterrupt, остановка автоответчика.")
            break


if __name__ == "__main__":
    main()
