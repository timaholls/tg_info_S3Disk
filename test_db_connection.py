import pymysql
from datetime import datetime

# Те же настройки что и в main.py
DB_HOST = 'localhost'
DB_PORT = 3307
DB_NAME = 's3_manager_local'
DB_USER = 's3_user'
DB_PASSWORD = 'Kfleirb_17$_'

TELEGRAM_ID = '323049682'

def get_db_connection():
    """Получить подключение к БД"""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

def test_connection():
    """Проверка подключения к БД"""
    print("=" * 60)
    print("ТЕСТ ПОДКЛЮЧЕНИЯ К БАЗЕ ДАННЫХ")
    print("=" * 60)
    print(f"Host: {DB_HOST}")
    print(f"Port: {DB_PORT}")
    print(f"Database: {DB_NAME}")
    print(f"User: {DB_USER}")
    print()
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                version = cursor.fetchone()
                print(f"✅ Подключение успешно!")
                print(f"MySQL версия: {version}")
                print()
                return True
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        print()
        return False

def check_user_exists():
    """Проверка существует ли пользователь в системе"""
    print("=" * 60)
    print("ПРОВЕРКА СУЩЕСТВУЮЩЕГО ПОЛЬЗОВАТЕЛЯ")
    print("=" * 60)
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, first_name, last_name, middle_name, region, email, telegram_id
                    FROM s3app_user
                    WHERE telegram_id = %s
                    LIMIT 1
                    """,
                    (TELEGRAM_ID,),
                )
                user = cursor.fetchone()
                
                if user:
                    # Получаем отделы пользователя
                    cursor.execute(
                        """
                        SELECT g.name
                        FROM auth_group g
                        JOIN s3app_user_groups ug ON g.id = ug.group_id
                        WHERE ug.user_id = %s
                        ORDER BY g.name
                        """,
                        (user['id'],),
                    )
                    user_departments = [row['name'] for row in cursor.fetchall()]
                    
                    print(f"✅ Пользователь найден в системе!")
                    print(f"   ID: {user['id']}")
                    print(f"   ФИО: {user['last_name']} {user['first_name']} {user.get('middle_name', '')}")
                    print(f"   Email: {user.get('email', 'не указан')}")
                    print(f"   Регион: {user.get('region', 'не указан')}")
                    print(f"   Telegram ID: {user['telegram_id']}")
                    if user_departments:
                        print(f"   Отделы: {', '.join(user_departments)}")
                    else:
                        print(f"   Отделы: не назначены")
                    print()
                    return user
                else:
                    print(f"ℹ️  Пользователь НЕ найден в системе")
                    print(f"   Это нормально для нового пользователя")
                    print()
                    return None
    except Exception as e:
        print(f"❌ Ошибка при проверке пользователя: {e}")
        import traceback
        traceback.print_exc()
        print()
        return None

def get_latest_request():
    """Получить последнюю заявку (точно такой же запрос как в боте)"""
    print("=" * 60)
    print("ПОЛУЧЕНИЕ ПОСЛЕДНЕЙ ЗАЯВКИ (КАК В БОТЕ)")
    print("=" * 60)
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Запрос из функции _get_latest_request_sync
                cursor.execute(
                    """
                    SELECT id, status, region, is_additional, created_at
                    FROM s3app_userrequest
                    WHERE telegram_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (TELEGRAM_ID,),
                )
                request = cursor.fetchone()
                
                if not request:
                    print(f"ℹ️  Заявок не найдено для telegram_id = {TELEGRAM_ID}")
                    print()
                    return None
                
                # Преобразуем is_additional в bool как в боте
                request["is_additional"] = bool(request.get("is_additional"))
                
                # Получаем обработанные отделы
                cursor.execute(
                    """
                    SELECT g.name
                    FROM auth_group g
                    JOIN s3app_userrequest_processed_departments pd ON g.id = pd.group_id
                    WHERE pd.userrequest_id = %s
                    ORDER BY g.name
                    """,
                    (request["id"],),
                )
                request["processed_departments"] = [row["name"] for row in cursor.fetchall()]
                
                # Получаем запрошенные отделы
                cursor.execute(
                    """
                    SELECT g.name
                    FROM auth_group g
                    JOIN s3app_userrequest_departments d ON g.id = d.group_id
                    WHERE d.userrequest_id = %s
                    ORDER BY g.name
                    """,
                    (request["id"],),
                )
                request["departments"] = [row["name"] for row in cursor.fetchall()]
                
                # Выводим результат
                print(f"✅ Заявка найдена!")
                print(f"   ID заявки: {request['id']}")
                print(f"   Статус: {request['status']}")
                print(f"   Регион: {request.get('region') or 'не указан'}")
                print(f"   Дополнительная заявка: {'Да' if request['is_additional'] else 'Нет'}")
                print(f"   Создана: {request['created_at'].strftime('%d.%m.%Y %H:%M') if request.get('created_at') else '—'}")
                
                if request['departments']:
                    print(f"   Запрошенные отделы: {', '.join(request['departments'])}")
                else:
                    print(f"   Запрошенные отделы: не указаны")
                
                if request['processed_departments']:
                    print(f"   Обработанные отделы: {', '.join(request['processed_departments'])}")
                else:
                    print(f"   Обработанные отделы: нет")
                
                print()
                return request
                
    except Exception as e:
        print(f"❌ Ошибка при получении заявки: {e}")
        import traceback
        traceback.print_exc()
        print()
        return None

def get_all_requests():
    """Получить все заявки пользователя"""
    print("=" * 60)
    print("ВСЕ ЗАЯВКИ ПОЛЬЗОВАТЕЛЯ")
    print("=" * 60)
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, full_name, status, region, is_additional, created_at, processed_at
                    FROM s3app_userrequest
                    WHERE telegram_id = %s
                    ORDER BY created_at DESC
                    """,
                    (TELEGRAM_ID,),
                )
                requests = cursor.fetchall()
                
                if not requests:
                    print(f"ℹ️  Заявок не найдено")
                    print()
                    return []
                
                print(f"Найдено заявок: {len(requests)}")
                print()
                
                for i, req in enumerate(requests, 1):
                    print(f"{i}. Заявка №{req['id']}")
                    print(f"   ФИО: {req.get('full_name', 'не указано')}")
                    print(f"   Статус: {req['status']}")
                    print(f"   Регион: {req.get('region') or 'не указан'}")
                    print(f"   Создана: {req['created_at'].strftime('%d.%m.%Y %H:%M') if req.get('created_at') else '—'}")
                    if req.get('processed_at'):
                        print(f"   Обработана: {req['processed_at'].strftime('%d.%m.%Y %H:%M')}")
                    print()
                
                return requests
                
    except Exception as e:
        print(f"❌ Ошибка при получении списка заявок: {e}")
        import traceback
        traceback.print_exc()
        print()
        return []

def get_departments():
    """Получить список отделов (как в боте)"""
    print("=" * 60)
    print("СПИСОК ОТДЕЛОВ В СИСТЕМЕ")
    print("=" * 60)
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name FROM auth_group ORDER BY name")
                departments = cursor.fetchall()
                
                if departments:
                    print(f"Найдено отделов: {len(departments)}")
                    print()
                    for i, dept in enumerate(departments, 1):
                        print(f"{i}. {dept['name']} (ID: {dept['id']})")
                    print()
                else:
                    print("⚠️  Отделов не найдено в системе!")
                    print()
                
                return departments
                
    except Exception as e:
        print(f"❌ Ошибка при получении отделов: {e}")
        print()
        return []

if __name__ == "__main__":
    print()
    print("🔍 ТЕСТИРОВАНИЕ ЗАПРОСОВ К БАЗЕ ДАННЫХ")
    print(f"Проверка для telegram_id = {TELEGRAM_ID}")
    print()
    
    # 1. Проверка подключения
    if not test_connection():
        print("❌ Не удалось подключиться к базе данных. Проверьте настройки.")
        exit(1)
    
    # 2. Проверка пользователя
    user = check_user_exists()
    
    # 3. Получение последней заявки (как делает бот)
    latest_request = get_latest_request()
    
    # 4. Все заявки
    all_requests = get_all_requests()
    
    # 5. Список отделов
    departments = get_departments()
    
    print("=" * 60)
    print("ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
    print("=" * 60)
    print()
    
    # Резюме
    if latest_request:
        print("✅ Бот должен найти заявку и показать её статус")
    elif user:
        print("✅ Бот должен предложить подать заявку на дополнительные отделы")
    else:
        print("✅ Бот должен начать процесс создания новой заявки")
    print()

