import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException

from app.publishing import create_super_admin_account


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    print("\nKOKWET SUPER ADMIN SETUP\n")
    email = input("Super Admin email: ").strip()
    phone = input("Super Admin phone number: ").strip()
    password = getpass.getpass("Super Admin password (minimum 16 characters): ")
    confirmation = getpass.getpass("Confirm password: ")

    if password != confirmation:
        print("Error: passwords do not match.", file=sys.stderr)
        raise SystemExit(1)

    try:
        staff_id = create_super_admin_account(email, phone, password)
    except (HTTPException, ValueError) as error:
        detail = error.detail if isinstance(error, HTTPException) else str(error)
        print(f"Error: {detail}", file=sys.stderr)
        raise SystemExit(1) from error

    print("\nSuper Admin 1 created.")
    print(f"Phone: {phone}")
    print(f"Sign in: http://localhost:5173/login")
    print("The password was stored as a server-side hash and is not printed.")
    print(f"Account reference: {staff_id}")


if __name__ == "__main__":
    main()
