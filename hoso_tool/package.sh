#!/usr/bin/env bash
# Đóng gói tool để copy sang máy khác (KHÔNG kèm venv, secrets, cache).
# Dùng:  bash hoso_tool/package.sh   -> tạo hoso_tool_portable.tar.gz ở thư mục gốc
set -e
cd "$(dirname "$0")/.."

OUT="hoso_tool_portable.tar.gz"
tar czf "$OUT" \
  --exclude='hoso_tool/__pycache__' \
  --exclude='hoso_tool/key.py' \
  --exclude='hoso_tool/.gemini_key' \
  hoso_tool .streamlit

echo "Đã tạo: $(pwd)/$OUT  ($(du -h "$OUT" | cut -f1))"
echo "Copy file này sang máy mới, giải nén rồi chạy:  bash hoso_tool/setup.sh"
