# -*- coding: utf-8 -*-
"""
Вывод последних писем во всех доступных IMAP-папках ящиков care и warranty.

Для каждой папки показывает до 5 самых свежих писем:
- UID
- Date
- Subject
- первые ~120 символов текста.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def decode_subject(subj_raw: str) -> str:
    from email.header import decode_header

    if not subj_raw:
        return ""
    parts = []
    for s, enc in decode_header(subj_raw):
        if isinstance(s, bytes):
            try:
                parts.append(s.decode(enc or "utf-8", errors="replace"))
            except Exception:
                parts.append(s.decode("utf-8", errors="replace"))
        else:
            parts.append(str(s))
    return "".join(parts)


def extract_body_preview(msg, max_len: int = 400) -> str:
    body_preview = ""
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                raw = p.get_payload(decode=True)
                if raw:
                    body_preview = raw.decode("utf-8", errors="replace")
                break
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            body_preview = raw.decode("utf-8", errors="replace")
    body_preview = body_preview.strip().replace("\r", " ").replace("\n", " ")
    if len(body_preview) > max_len:
        body_preview = body_preview[: max_len - 3] + "..."
    return body_preview


def list_last_messages_in_folder(imap, folder_name: str, limit: int = 5):
    import email

    try:
        status, _ = imap.select(folder_name)
    except Exception as e:
        print(f"\n[Папка] {folder_name!r} — не удалось открыть: {e}")
        return
    if status != "OK":
        print(f"\n[Папка] {folder_name!r} — не удалось открыть")
        return

    status, data = imap.search(None, "ALL")
    if status != "OK":
        print(f"\n[Папка] {folder_name!r} — ошибка поиска")
        return
    if not data or not data[0]:
        return

    uids = data[0].split()
    last_uids = uids[-limit:]
    print(f"\n[Папка] {folder_name!r} (показаны последние {len(last_uids)})")
    for uid in last_uids:
        uid_s = uid.decode() if isinstance(uid, bytes) else uid
        status, msg_data = imap.fetch(uid_s, "(RFC822)")
        if status != "OK":
            print(f"- UID={uid_s}: ошибка fetch: {status}")
            continue
        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            msg = email.message_from_bytes(part[1])
            date = msg.get("Date", "")
            subj = decode_subject(msg.get("Subject", ""))
            body_preview = extract_body_preview(msg)
            print(f"- UID={uid_s}, Date={date}, Subject={subj!r}")
            if body_preview:
                print(f"  Тело: {body_preview}")
            break


def list_last_messages(login: str, password: str, name: str, limit: int = 2):
    import imaplib

    print(f"\n=== Ящик {name} ({login}) ===")
    try:
        imap = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
        imap.login(login, password)
    except Exception as e:
        print(f"IMAP ошибка входа: {e}")
        return

    try:
        status, folders_raw = imap.list()
        if status != "OK" or not folders_raw:
            print("Не удалось получить список папок.")
            return

        folder_names = []
        for row in folders_raw:
            line = row.decode(errors="replace")
            # Формат LIST обычно: (<flags>) "<delimiter>" "<mailbox>"
            # Берём mailbox как последнюю часть после '"<delimiter>" '.
            try:
                parts = line.split('" "', 1)
                if len(parts) == 2:
                    folder_name = parts[1].strip()
                else:
                    folder_name = line.split()[-1].strip()
            except Exception:
                folder_name = line
            # Снимаем внешние кавычки, если есть.
            folder_name = folder_name.strip()
            if folder_name.startswith('"') and folder_name.endswith('"'):
                folder_name = folder_name[1:-1]
            if folder_name:
                folder_names.append(folder_name)

        # Чтобы чаще нужное было выше, но всё равно проход по всем папкам.
        folder_names = sorted(set(folder_names), key=lambda x: (x.upper() != "INBOX", x.lower()))
        print(f"Найдено папок: {len(folder_names)}")
        for folder in folder_names:
            list_last_messages_in_folder(imap, folder, limit=limit)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main():
    care_user = os.getenv("MAIL_USER_CARE", "").strip()
    care_pass = os.getenv("MAIL_PASSWORD_CARE", "").strip()
    warranty_user = os.getenv("MAIL_USER_WARRANTY", "").strip()
    warranty_pass = os.getenv("MAIL_PASSWORD_WARRANTY", "").strip()

    if not any([care_user and care_pass, warranty_user and warranty_pass]):
        print("Не заданы данные для ящиков в .env.")
        return

    if care_user and care_pass:
        list_last_messages(care_user, care_pass, "care")
    else:
        print("Ящик care не настроен (MAIL_USER_CARE / MAIL_PASSWORD_CARE).")

    if warranty_user and warranty_pass:
        list_last_messages(warranty_user, warranty_pass, "warranty")
    else:
        print("Ящик warranty не настроен (MAIL_USER_WARRANTY / MAIL_PASSWORD_WARRANTY).")


if __name__ == "__main__":
    main()

