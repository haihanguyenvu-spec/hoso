#!/usr/bin/env bash
# Cài đặt nhanh tool trên máy mới (Linux / macOS).
# Dùng:  bash setup.sh
set -e
cd "$(dirname "$0")/.."          # về thư mục gốc (chứa hoso_tool/)

echo "==> Kiểm tra python3"
command -v python3 >/dev/null || { echo "Thiếu python3. Cài python3 trước."; exit 1; }

echo "==> Kiểm tra poppler (pdfinfo/pdftoppm/pdfunite)"
if ! command -v pdfunite >/dev/null; then
  echo "!! Thiếu poppler. Cài bằng:"
  echo "     Ubuntu/Debian:  sudo apt install -y poppler-utils"
  echo "     macOS (brew):   brew install poppler"
  exit 1
fi

echo "==> Tạo virtualenv .venv"
python3 -m venv .venv

echo "==> Cài thư viện Python"
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r hoso_tool/requirements.txt

echo ""
echo "XONG! Chạy giao diện bằng:"
echo "     .venv/bin/streamlit run hoso_tool/app.py"
