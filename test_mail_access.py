# -*- coding: utf-8 -*-
"""Проверка доступа по IMAP и SMTP для ящиков из .env."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def test_imap(login: str, password: str, name: str = "Ящик") -> bool:
    import imaplib
    try:
        imap = imaplib.IMAP4_SSL("imap.yandex.ru", 993)
        imap.login(login, password)
        imap.logout()
        print(f"  IMAP ({name}): OK")
        return True
    except Exception as e:
        print(f"  IMAP ({name}): Ошибка — {e}")
        return False


def test_smtp(login: str, password: str, name: str = "Ящик") -> bool:
    import smtplib
    try:
        with smtplib.SMTP("smtp.yandex.ru", 587) as smtp:
            smtp.starttls()
            smtp.login(login, password)
        print(f"  SMTP ({name}): OK")
        return True
    except Exception as e:
        print(f"  SMTP ({name}): Ошибка — {e}")
        return False


def main():
    care_user = os.getenv("MAIL_USER_CARE", "").strip()
    care_pass = os.getenv("MAIL_PASSWORD_CARE", "").strip()
    warranty_user = os.getenv("MAIL_USER_WARRANTY", "").strip()
    warranty_pass = os.getenv("MAIL_PASSWORD_WARRANTY", "").strip()

    ok = True

    if care_user and care_pass:
        print("Care (обращения):")
        if not test_imap(care_user, care_pass, "care"):
            ok = False
        if not test_smtp(care_user, care_pass, "care"):
            ok = False
    else:
        print("Care: MAIL_USER_CARE или MAIL_PASSWORD_CARE не заданы.")

    if warranty_user and warranty_pass:
        print("Warranty (регистрация):")
        if not test_imap(warranty_user, warranty_pass, "warranty"):
            ok = False
        if not test_smtp(warranty_user, warranty_pass, "warranty"):
            ok = False
    else:
        print("Warranty: MAIL_USER_WARRANTY или MAIL_PASSWORD_WARRANTY не заданы.")

    print()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
