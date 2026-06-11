#!/bin/bash

# 에러 발생 시 즉시 중단
set -e

# 장고 소스가 있는 data 폴더로 이동
cd /app/data

# 시스템 패키지 설치 (ping, gettext)
if ! command -v ping &> /dev/null || ! command -v msgfmt &> /dev/null; then
    echo "Installing system dependencies (ping, gettext)..."
    apt-get update && apt-get install -y iputils-ping gettext
fi

# 1. .venv 폴더가 없으면 가상환경 생성
if [ ! -d ".venv" ]; then
    echo "Creating Python 3.12 virtual environment..."
    python -m venv .venv
fi

# 가상환경 활성화
source .venv/bin/activate

# 2. 패키지 자동 설치
echo "Installing/Updating dependencies from requirements.txt..."
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    echo "Error: requirements.txt not found!"
    exit 1
fi

# 3. 장고 로그 폴더 생성
if [ ! -d "logs" ]; then
    echo "Creating logs directory..."
    mkdir -p logs
fi

# 4. MariaDB 부팅 대기 (접속 정보는 환경변수에서 읽음)
echo "Waiting for MariaDB to start..."
until python -c "
import sys, os, pymysql
try:
    pymysql.connect(
        host=os.getenv('DB_HOST', 'mariadb'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        port=int(os.getenv('DB_PORT', '3306')),
    )
except Exception:
    sys.exit(-1)
" 2>/dev/null; do
    echo "MariaDB is unavailable - sleeping..."
    sleep 2
done
echo "MariaDB is up!"

# 5. 마이그레이션 적용
if [ "${RUN_MAKEMIGRATIONS:-False}" = "True" ]; then
    echo "Generating migrations because RUN_MAKEMIGRATIONS=True..."
    python manage.py makemigrations --noinput
fi
echo "Applying database migrations..."
python manage.py migrate --noinput

# 5-1. 관리자 계정 생성 (명시적인 환경변수가 있을 때만 수행)
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    echo "Ensuring superuser '${DJANGO_SUPERUSER_USERNAME}' exists..."
    python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
User = get_user_model()
username = os.environ['DJANGO_SUPERUSER_USERNAME']
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, os.environ['DJANGO_SUPERUSER_EMAIL'], os.environ['DJANGO_SUPERUSER_PASSWORD'])
    print(f'Superuser {username} created.')
else:
    print(f'Superuser {username} already exists.')
"
else
    echo "Skipping superuser creation because DJANGO_SUPERUSER_* is not fully configured."
fi

# 6. 번역 파일 컴파일
echo "Compiling translation messages..."
python manage.py compilemessages

# 7. 정적 파일 수집
echo "Collecting static files..."
python manage.py collectstatic --noinput

# 8. Celery Worker/Beat 실행 (백그라운드)
echo "Starting Celery Worker..."
celery -A config worker -l info &

echo "Starting Celery Beat..."
celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler &

# 8-1. 유효하지 않은 셀러리 스케줄 및 큐 자동 정리 (롤백 후 잔재 제거)
echo "Cleaning up obsolete celery tasks..."
python manage.py shell -c "from django_celery_beat.models import PeriodicTask; PeriodicTask.objects.filter(name__in=['process-metric-batches-every-5-seconds', 'sync-metadata-every-30-seconds']).delete(); from config.celery import app; app.control.purge()"

# 9. 장고 서버 실행
echo "Starting Django server on port ${DJANGO_PORT} (DEBUG=${DEBUG})..."

if [ "${DEBUG}" = "True" ]; then
    echo "Running in Development mode (runserver)..."
    python manage.py runserver 0.0.0.0:${DJANGO_PORT}
else
    echo "Running in Production mode (daphne)..."
    daphne -b 0.0.0.0 -p ${DJANGO_PORT} config.asgi:application
fi
