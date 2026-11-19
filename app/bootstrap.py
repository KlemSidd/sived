from app import config
from app.db import open_db
from app.services import create_owner, seed_local_accounts


def main() -> None:
    conn = open_db()
    if config.DEV_LOGIN or config.DEMO_MODE:
        seeded = seed_local_accounts(conn)
        print(seeded["owner_id"])
        print("owner: owner@example.com")
        print("author: author@example.com")
        print(f"author page: /e/{seeded['editor_id']}")
    else:
        print(create_owner(conn, "owner@example.com"))
    conn.close()


if __name__ == "__main__":
    main()
