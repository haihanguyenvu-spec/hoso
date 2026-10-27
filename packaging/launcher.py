"""Khởi động HoSoPDF: chạy Streamlit ẩn, chờ server lên rồi mở trình duyệt.

Shortcut gọi:  python\\pythonw.exe launcher.py  (pythonw = không có cửa sổ console).
Thay cho launch.vbs cũ: Microsoft đang gỡ dần VBScript khỏi Windows, máy đã tắt VBScript
sẽ không mở được app. File này nằm ở thư mục cài, cùng cấp với python\\, poppler\\,
tesseract\\, app\\. Chỉ dùng thư viện chuẩn (Python embeddable không thêm script dir vào
sys.path).
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
# Không dùng 8501: đó là cổng mặc định của MỌI app Streamlit khác trên máy -> dễ mở nhầm app.
PORT = 8517
URL = f"http://127.0.0.1:{PORT}"
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "HoSoPDF")
LOG_FILE = os.path.join(DATA_DIR, "app_log.txt")
LOCK_FILE = os.path.join(DATA_DIR, "launcher.lock")
# Lần đầu sau khi cài, Windows Defender quét toàn bộ thư viện -> có thể mất cả phút.
WAIT_SECONDS = 180

# Bỏ qua proxy hệ thống: proxy công ty không chuyển tiếp 127.0.0.1 -> tưởng server chưa lên.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_up() -> bool:
    try:
        with _opener.open(URL + "/_stcore/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def open_browser():
    webbrowser.open(URL)


def show_error(text: str):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "HoSoPDF", 0x10)  # MB_ICONERROR
    else:
        print(text, file=sys.stderr)


def log_tail(n: int = 15) -> str:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).strip()
    except OSError:
        return ""


def acquire_lock():
    """Khóa 'đang khởi động'. None nếu một lần bấm icon khác đang giữ khóa.

    Người dùng hay bấm icon nhiều lần vì lần đầu phải chờ lâu — không có khóa thì mỗi lần
    bấm dựng thêm một server, các server sau lỗi trùng cổng và báo lỗi oan.
    """
    f = open(LOCK_FILE, "a+")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def start_server() -> subprocess.Popen:
    env = os.environ.copy()
    # poppler (pdfinfo, pdftoppm, ...) và tesseract được gọi bằng tên trần -> phải có trên
    # PATH; đặt TRƯỚC PATH sẵn có để bản đi kèm thắng mọi bản khác đã cài trên máy.
    env["PATH"] = os.pathsep.join([os.path.join(BASE, "poppler", "Library", "bin"),
                                   os.path.join(BASE, "tesseract"), env.get("PATH", "")])
    # Chỉ rõ dữ liệu nhận dạng của bản đi kèm: TESSDATA_PREFIX của một Tesseract khác
    # (thiếu osd/eng) sẽ làm hỏng âm thầm chức năng sửa trang xoay.
    env["TESSDATA_PREFIX"] = os.path.join(BASE, "tesseract", "tessdata")
    # stdout ghi ra file: không có cờ này Python dùng bảng mã ANSI (cp1252) và mọi dòng in
    # tiếng Việt sẽ ném UnicodeEncodeError.
    env["PYTHONUTF8"] = "1"
    app_dir = os.path.join(BASE, "app")
    python = os.path.join(BASE, "python", "python.exe" if os.name == "nt" else "python")
    cmd = [python, "-m", "streamlit", "run", os.path.join(app_dir, "hoso_tool", "app.py"),
           "--server.headless=true", f"--server.port={PORT}",
           # Chỉ nghe trên máy này: không bật hộp thoại Windows Firewall, và máy khác trong
           # mạng LAN không mở được app (app chứa API key và đường dẫn hồ sơ).
           "--server.address=127.0.0.1",
           "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]
    try:
        log = open(LOG_FILE, "w", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    # CREATE_NO_WINDOW (không phải DETACHED_PROCESS): server có console ẩn để các lệnh con
    # (pdftoppm, tesseract) dùng chung, không mỗi lệnh bật lên một cửa sổ đen.
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        # Streamlit đọc .streamlit\config.toml theo thư mục làm việc -> cwd = app\.
        return subprocess.Popen(cmd, cwd=app_dir, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def wait_until_up(deadline: float, proc: subprocess.Popen | None = None) -> str:
    """'up' khi server trả lời, 'exited' nếu proc đã chết, 'timeout' khi hết giờ."""
    while time.time() < deadline:
        if server_up():
            return "up"
        if proc is not None and proc.poll() is not None:
            return "exited"
        time.sleep(1)
    return "timeout"


def main():
    # Đang chạy sẵn (bấm icon lần nữa) -> chỉ mở trình duyệt.
    if server_up():
        open_browser()
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    deadline = time.time() + WAIT_SECONDS
    lock = acquire_lock()
    if lock is None:
        # Một lần bấm khác đang khởi động server -> chỉ chờ rồi mở trình duyệt.
        wait_until_up(deadline)
        open_browser()
        return
    try:
        if server_up():         # lần bấm trước vừa khởi động xong
            open_browser()
            return
        try:
            proc = start_server()
        except OSError as e:
            show_error(f"Không chạy được HoSoPDF ({e}).\n\nHãy cài lại phần mềm.")
            return
        state = wait_until_up(deadline, proc)
        if state == "up":
            open_browser()
        elif state == "exited":
            show_error("HoSoPDF không khởi động được.\n\n"
                       f"{log_tail()}\n\n"
                       f"Gửi file nhật ký này cho người hỗ trợ:\n{LOG_FILE}")
        else:
            show_error("HoSoPDF khởi động lâu hơn bình thường. Trình duyệt sẽ mở ngay sau đây —"
                       " nếu trang báo không kết nối được, đợi thêm một lúc rồi bấm F5.\n\n"
                       f"Nếu vẫn lỗi, gửi file nhật ký cho người hỗ trợ:\n{LOG_FILE}")
            open_browser()
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # pythonw không có console: lỗi không báo ra là mất dấu vết
        show_error(f"HoSoPDF gặp lỗi khi khởi động: {type(e).__name__}: {e}")
