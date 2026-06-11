# AgnMonitor

[English](README.md) | 한국어

AgnMonitor는 Telegraf 에이전트로부터 메트릭과 로그를 수집하고, 서버 상태를 시각화하며, 알림을 제공하는 Django 기반 모니터링 대시보드입니다.

## 주요 기능

- HTTP 엔드포인트를 통한 Telegraf 메트릭 수집
- 호스트, 접속 IP, API 수집 상태, Ping 상태 추적
- 서버별 커스텀 대시보드 패널 구성
- 수집 데이터, 로그, 서버 상태 요약 조회
- Linux, Windows, 서버별 데이터 수집 규칙 설정
- 임계치, 상태 체크, 호스트 다운, 로그, 그룹 대상 알림 규칙 설정
- Celery 태스크 기반 이메일/웹훅 알림 발송
- 오래된 메트릭 집계 및 보존 정책 기반 정리
- 한국어/영어 UI 번역

## 기술 스택

- Django 6, Django Channels, Daphne
- MariaDB, PyMySQL
- Valkey: Redis 호환 cache/pubsub/Celery broker
- Celery, django-celery-beat
- Telegraf HTTP output
- Bootstrap, GridStack, HTMX, Vanilla JavaScript
- Docker Compose

## 빠른 시작

```bash
cp data/.env.example data/.env
# data/.env를 열어 DB, 이메일, 관리자 계정 값을 실제 값으로 설정합니다.

docker compose up -d
```

기동 후 접속:

- 애플리케이션: `http://<HOST>:18080`
- Django Admin: `http://<HOST>:18080/admin/`
- 수집 엔드포인트: `http://<HOST>:18080/api/collect/`

최초 기동 시 `data/.env`의 `DJANGO_SUPERUSER_*` 값으로 관리자 계정이 1회 자동 생성됩니다.

## Telegraf 에이전트

수집 대상 서버에 `data/telegraf.conf`를 배포하고 HTTP output URL을 실제 AgnMonitor 주소로 수정하세요.

```toml
[[outputs.http]]
  url = "http://<MONITOR_HOST>:18080/api/collect/"
```

## 설정

런타임 설정은 `data/.env`에서 로드합니다. `data/.env.example`을 복사해서 사용하세요.

| 분류 | 변수 |
| --- | --- |
| Django | `SECRET_KEY`, `DEBUG`, `DJANGO_SUPERUSER_*` |
| 데이터베이스 | `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `MARIADB_*` |
| Cache/Broker | `REDIS_URL` |
| Email | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` |
| 접근 제어 | `ADMIN_ALLOWED_NETWORKS`, `CSRF_TRUSTED_SUBNETS`, `CSRF_TRUSTED_PORTS`, `CSRF_TRUSTED_ORIGINS_EXTRA` |

## 프로젝트 구조

```text
AgnMonitor/
├── docker-compose.yml
├── nginx.conf
├── load_test_monitor.py
├── load_test_telegraf_simulator.py
├── data/
│   ├── config/             # Django 설정, ASGI/WSGI, Celery
│   ├── core_dashboard/     # 메트릭, 대시보드, 알림, consumers, tasks
│   ├── templates/          # 공통 템플릿
│   ├── static/             # 정적 파일 소스
│   ├── locale/             # i18n 번역 파일
│   └── telegraf.conf       # Telegraf 에이전트 예시 설정
└── make_deploy.sh
```

## 보안 주의

다음 런타임 비밀값과 생성 데이터는 커밋하지 마세요.

- `data/.env`
- `data/.secret_key`
- `data/core_dashboard/.secret.key`
- `mariadb_data/`
- `valkey_data/`
- `backup/`
- `logs/`

운영 환경에서는 관리자/DB 비밀번호를 강하게 설정하고, SMTP 인증 정보는 환경변수로 관리하며, `DEBUG=False`로 실행하세요.

## 라이선스

이 프로젝트는 MIT License로 배포됩니다. 자세한 내용은 [LICENSE](LICENSE)를 참고하세요.
