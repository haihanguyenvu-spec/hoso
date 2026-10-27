"""Dừng HoSoPDF: tắt đúng tiến trình python.exe của bản cài này (không đụng Python khác).

Shortcut "Dừng HoSoPDF" và bước gỡ cài đặt gọi:  python\\pythonw.exe stop.py
Thay cho stop.vbs cũ (Microsoft đang gỡ dần VBScript khỏi Windows).
"""
import os
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(BASE, "python", "python.exe")

# Lọc theo ĐƯỜNG DẪN exe (không theo tên) để không tắt nhầm Python của chương trình khác;
# taskkill /T tắt luôn các lệnh con đang chạy dở (pdftoppm, tesseract) để gỡ cài đặt không
# vướng file đang bị khóa. stop.py chạy bằng pythonw.exe nên không tự tắt chính nó.
PS = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
      "Where-Object { $_.ExecutablePath -eq '" + TARGET.replace("'", "''") + "' } | "
      "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }")

if __name__ == "__main__":
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=120)
