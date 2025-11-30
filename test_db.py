from app.db import get_sync_session

def main():
    with get_sync_session() as session:
        result = session.execute("SELECT 1")
        print("DB OK:", list(result))

if __name__ == "__main__":
    main()
