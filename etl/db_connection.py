import psycopg2

def get_connection():
    conn =  psycopg2.connect(
            host="localhost",
            database="postgres",
            user="postgres",
            password="010506",
            port=5432)
    
    with conn.cursor() as cursor:
            cursor.execute("SET search_path TO risk_platform;")
            
    return conn

try:
    conn = get_connection()
    print("✅ Connected to PostgreSQL successfully!")
    conn.close()
except Exception as e:
    print("❌ Connection failed!")
    print(e)