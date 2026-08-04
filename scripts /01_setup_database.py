from pathlib import Path
from psycopg2 import connect

# SQL directory
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# SQL files in execution order
SQL_FILES = [
    "01_schema.sql",
    "02_dimension.sql",
    "03_market_data.sql",
    "04_history.sql",
    "05_analysis.sql",
    "06_constraint.sql",
]


def get_connection():
    return connect(
        host="localhost",
        database="postgres",
        user="postgres",
        password="010506",
        port=5432
    )


def execute_sql_file(cursor, file_path):
    print(f"Running {file_path.name}")

    with open(file_path, "r", encoding="utf-8") as f:
        cursor.execute(f.read())


def run_database_setup():

    conn = get_connection()

    try:
        with conn.cursor() as cursor:

            for file in SQL_FILES:
                execute_sql_file(cursor, SQL_DIR / file)

        conn.commit()

        print("\nDatabase setup completed successfully.")

    except Exception as e:

        conn.rollback()
        print(f"\nError while executing SQL scripts:\n{e}")
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    run_database_setup()