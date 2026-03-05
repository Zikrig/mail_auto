# -*- coding: utf-8 -*-
"""
Вывод последних писем в ящиках care и warranty.

Показывает для каждого ящика до 5 самых «свежих» писем из INBOX:
- UID
- Date
- Subject
- первые ~120 символов текста.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def list_last_messages(login: str, password: str, name: str, limit: int = 5):
    import imaplib
    import email
    from email.header import decode_header

    print(f"\n=== Ящик {name} ({login}) ===")
    try:
        imap = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
        imap.login(login, password)
    except Exception as e:
        print(f"IMAP ошибка входа: {e}")
        return

    try:
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            print("Писем не найдено.")
            return
        uids = data[0].split()
        last_uids = uids[-limit:]
        for uid in last_uids:
            uid_s = uid.decode() if isinstance(uid, bytes) else uid
            status, msg_data = imap.fetch(uid_s, "(RFC822)")
            if status != "OK":
                print(f"UID={uid_s}: ошибка fetch: {status}")
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                date = msg.get("Date", "")
                subj_raw = msg.get("Subject", "")
                if subj_raw:
                    dh = decode_header(subj_raw)
                    s, enc = dh[0]
                    if isinstance(s, bytes):
                        try:
                            subj = s.decode(enc or "utf-8", errors="replace")
                        except Exception:
                            subj = s.decode("utf-8", errors="replace")
                    else:
                        subj = s
                else:
                    subj = ""
                # Берём текстовую часть тела
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
                if len(body_preview) > 120:
                    body_preview = body_preview[:117] + "..."
                print(f"- UID={uid_s}, Date={date}, Subject={subj!r}")
                if body_preview:
                    print(f"  Тело: {body_preview}")
                break
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

