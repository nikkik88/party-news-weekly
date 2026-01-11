#!/usr/bin/env python3
"""
진보당 전용 크롤러
- GitHub Actions에서 진보당 접속이 차단되어 로컬에서만 실행
- Windows/macOS 부팅 시 자동 실행용
"""

import subprocess
import sys
from datetime import datetime


def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] 진보당 크롤링 시작...")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "src/main.py",
                "--only", "jinboparty",
                "--sample", "10",
                "--notion",
                "--date-from", "2026-01-01",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        print(result.stdout)
        if result.stderr:
            print("경고/에러:", result.stderr)

        if result.returncode == 0:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 진보당 크롤링 완료")
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 진보당 크롤링 실패")

    except Exception as e:
        print(f"에러 발생: {e}")


if __name__ == "__main__":
    main()
