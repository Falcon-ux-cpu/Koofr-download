import os
import time
import re
import requests
import smtplib
from email.mime.text import MIMEText
from imap_tools import MailBox, AND
from datetime import datetime

# Чтение настроек из Secrets
CONFIG = {
    "imap_server": "imap.gmail.com",
    "email_user": os.getenv("EMAIL_USER"),
    "email_pass": os.getenv("EMAIL_PASS"),
    "koofr_user": os.getenv("KOOFR_USER"),
    "koofr_pass": os.getenv("KOOFR_PASS"),
    "target_notification": os.getenv("TARGET_NOTIFICATION"),
    "sender_filter": os.getenv("SENDER_FILTER", os.getenv("TARGET_NOTIFICATION")),
}

WEBDAV_BASE = "https://app.koofr.net/dav/Koofr"

def clean_name(name):
    if not name:
        return "unnamed"
    match = re.search(r'[\x00-\x1F\x7F/\\:\*\?"<>|\']', name)
    if match:
        name = name[:match.start()]
    return name.strip() or "unnamed"

def create_koofr_folder(folder_path):
    parts = [clean_name(p) for p in folder_path.strip('/').split('/') if p]
    current = ""
    auth = (CONFIG["koofr_user"], CONFIG["koofr_pass"])
    for part in parts:
        current = f"{current}/{part}" if current else part
        url = f"{WEBDAV_BASE}/{current}"
        requests.request("MKCOL", url, auth=auth)
    return current

def get_file_size_mb(path):
    url = f"{WEBDAV_BASE}/{path}"
    auth = (CONFIG["koofr_user"], CONFIG["koofr_pass"])
    res = requests.head(url, auth=auth)
    if res.status_code == 200:
        size_bytes = int(res.headers.get('Content-Length', 0))
        return round(size_bytes / (1024 * 1024), 2)
    return 0

def mark_email_as_seen(uid):
    """Отдельное подключение для безопасной пометки письма как прочитанного"""
    try:
        with MailBox(CONFIG["imap_server"]).login(CONFIG["email_user"], CONFIG["email_pass"]) as mailbox:
            mailbox.flag(uid, ['SEEN'], True)
    except Exception as e:
        print(f"Не удалось пометить письмо {uid} прочитанным: {e}")

def send_final_notification(filename, size_mb):
    body = (
        f"✅ Загрузка на Koofr завершена!\n\n"
        f"📁 Файл: {filename}\n"
        f"⚖️ Размер: {size_mb} MB\n"
    )
    msg = MIMEText(body)
    msg['Subject'] = f"Завершено: {filename}"
    msg['From'] = CONFIG["email_user"]
    msg['To'] = CONFIG["target_notification"]
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(CONFIG["email_user"], CONFIG["email_pass"])
            server.send_message(msg)
    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")

def parse_content(text):
    text = re.sub('<[^<]+?>', '', text).replace('\r', '').strip()
    
    url_match = re.search(r'https?://[^\s]+', text)
    if not url_match:
        return None, None, None, None
    url = url_match.group(0).strip()
    
    login_match = re.search(r'Login:\s*(\S+)', text)
    pass_match = re.search(r'Pass:\s*(\S+)', text)
    
    login = login_match.group(1) if login_match else None
    password = pass_match.group(1) if pass_match else None

    # Оригинальные имя и расширение из URL
    url_clean = url.split('?')[0]
    orig_basename = url_clean.split('/')[-1]
    orig_name, orig_ext = os.path.splitext(orig_basename)

    # Поиск новых переменных с приоритетом
    folder_match = re.search(r'^Папка:\s*(.+)$', text, re.MULTILINE)
    path_match = re.search(r'^Путь:\s*(.+)$', text, re.MULTILINE)
    name_match = re.search(r'^Название:\s*(.+)$', text, re.MULTILINE)
    ext_match = re.search(r'^Расширение:\s*(.+)$', text, re.MULTILINE)

    c_folder = folder_match.group(1).strip() if folder_match else None
    c_path = path_match.group(1).strip() if path_match else None
    c_name = name_match.group(1).strip() if name_match else None
    c_ext = ext_match.group(1).strip() if ext_match else None

    if c_ext and not c_ext.startswith('.'):
        c_ext = f".{c_ext}"
    final_ext = c_ext if c_ext else orig_ext

    # Старая дефолтная логика (сериал, фильм или текст после ссылки)
    default_folder = None
    default_filename = None

    series_match = re.search(r'Смотреть (.+?) Сезон (\d+) - Эпизод (\d+)', text)
    if series_match:
        title, s_num, e_num = series_match.groups()
        default_folder = create_koofr_folder(title.strip())
        default_filename = f"{int(s_num):02d}-{int(e_num):02d}{final_ext}"
    else:
        movie_match = re.search(r'Смотреть (.+?) (?:Дубляж|1080p|720p|\d+p)', text)
        if movie_match:
            title = clean_name(movie_match.group(1).strip())
            default_filename = f"{title}{final_ext}"
        else:
            after_url = text[url_match.end():].strip()
            after_url_clean = re.sub(r'(Login|Pass|Папка|Путь|Название|Расширение):\s*[^\n]+', '', after_url, flags=re.IGNORECASE).strip()
            if after_url_clean:
                first_line = after_url_clean.split('\n')[0].strip()
                default_filename = f"{clean_name(first_line)}{final_ext}"
            else:
                default_filename = f"{clean_name(orig_name)}{final_ext}" if orig_name else f"unnamed{final_ext}"

    # Итоговое имя файла
    final_filename = f"{clean_name(c_name)}{final_ext}" if c_name else (default_filename if default_filename else f"unnamed{final_ext}")

    # Обработка Пути и Папки
    if c_path or c_folder:
        path_parts = []
        if c_path:
            path_parts.extend([p for p in c_path.strip('/').split('/') if p])
        if c_folder:
            path_parts.append(clean_name(c_folder))
        
        full_folder_path = "/".join(path_parts)
        created_folder = create_koofr_folder(full_folder_path)
        return url, f"{created_folder}/{final_filename}", login, password
    else:
        if series_match and default_folder:
            return url, f"{default_folder}/{final_filename}", login, password
        else:
            return url, final_filename, login, password

def upload_via_koofr(url, koofr_path, login=None, password=None):
    target_url = f"{WEBDAV_BASE}/{koofr_path}"
    auth = (CONFIG["koofr_user"], CONFIG["koofr_pass"])
    src_auth = (login, password) if login and password else None
    
    try:
        print(f"Загрузка начата на Koofr (Auth: {'Да' if login else 'Нет'})...")
        
        with requests.get(url, auth=src_auth, stream=True, timeout=120) as src_res:
            if src_res.status_code != 200:
                print(f"Ошибка скачивания источника: HTTP {src_res.status_code}")
                return False
                
            put_res = requests.put(target_url, auth=auth, data=src_res.iter_content(chunk_size=65536))
            
            if put_res.status_code in [200, 201, 204]:
                print(f"Файл {koofr_path} успешно загружен на Koofr.")
                return True
            else:
                print(f"Ошибка загрузки на Koofr: HTTP {put_res.status_code} {put_res.text}")
                return False

    except Exception as e:
        print(f"Ошибка в процессе передачи файла: {e}")
        return False

def main():
    print(f"--- Старт сессии: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    tasks = []
    
    # Шаг 1: Быстро собираем все непрочитанные письма и сразу закрываем IMAP
    try:
        with MailBox(CONFIG["imap_server"]).login(CONFIG["email_user"], CONFIG["email_pass"]) as mailbox:
            messages = list(mailbox.fetch(AND(from_=CONFIG["sender_filter"], seen=False)))
            
            if not messages:
                print("Новых заданий в почте нет.")
                return

            print(f"Найдено писем для обработки: {len(messages)}")

            for msg in messages:
                content = msg.text or msg.html or ""
                f_url, f_path, f_login, f_pass = parse_content(content)
                if f_url and f_path:
                    tasks.append({
                        "uid": msg.uid,
                        "url": f_url,
                        "path": f_path,
                        "login": f_login,
                        "pass": f_pass
                    })
    except Exception as e:
        print(f"Ошибка чтения почты: {e}")
        return

    # Шаг 2: Последовательно скачиваем файлы независимо от длинных таймаутов IMAP
    for task in tasks:
        print(f"\nОбработка: {task['path']}")
        try:
            success = upload_via_koofr(task["url"], task["path"], task["login"], task["pass"])
            
            if success:
                size = get_file_size_mb(task["path"])
                send_final_notification(task["path"], size)
                # Помечаем прочитанным ТОЛЬКО после успешной загрузки
                mark_email_as_seen(task["uid"])
            else:
                print(f"Не удалось загрузить файл из письма UID:{task['uid']}")
        except Exception as err:
            print(f"Сбой при обработке задания {task['path']}: {err}")

    print(f"\n--- Сессия завершена ---")

if __name__ == "__main__":
    main()
