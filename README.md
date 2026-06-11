# AgnMonitor

English | [Korean](README.ko.md)

AgnMonitor is a Django-based monitoring dashboard that collects metrics and logs from Telegraf agents, stores selected data, visualizes server status, and sends alerts.

## Features

- Collect Telegraf metrics through an HTTP endpoint
- Track hosts, reachable IPs, API collection status, and ping status
- Build customizable dashboard panels per server
- View collected metric/log data and status summaries
- Configure data collection rules for Linux, Windows, or individual servers
- Define alert rules for thresholds, status checks, host-down events, logs, and groups
- Send email and webhook notifications through Celery tasks
- Aggregate old metrics and apply retention cleanup policies
- Korean and English UI translations

## Tech Stack

- Django 6, Django Channels, Daphne
- MariaDB with PyMySQL
- Valkey as Redis-compatible cache, pub/sub, and Celery broker
- Celery and django-celery-beat
- Telegraf HTTP output
- Bootstrap, GridStack, HTMX, and vanilla JavaScript
- Docker Compose

## Quick Start

```bash
cp data/.env.example data/.env
# Edit data/.env and set real database, email, and admin credentials.

docker compose up -d
```

After startup:

- Application: `http://<HOST>:18080`
- Django Admin: `http://<HOST>:18080/admin/`
- Collection endpoint: `http://<HOST>:18080/api/collect/`

The first superuser is created once from the `DJANGO_SUPERUSER_*` values in `data/.env`.

## Telegraf Agent

Deploy `data/telegraf.conf` to each monitored server and update the HTTP output URL:

```toml
[[outputs.http]]
  url = "http://<MONITOR_HOST>:18080/api/collect/"
```

## Configuration

All runtime configuration is loaded from `data/.env`. Start from `data/.env.example`.

Important groups:

| Area | Variables |
| --- | --- |
| Django | `SECRET_KEY`, `DEBUG`, `DJANGO_SUPERUSER_*` |
| Database | `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `MARIADB_*` |
| Cache/Broker | `REDIS_URL` |
| Email | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` |
| Access Control | `ADMIN_ALLOWED_NETWORKS`, `CSRF_TRUSTED_SUBNETS`, `CSRF_TRUSTED_PORTS`, `CSRF_TRUSTED_ORIGINS_EXTRA` |

## Project Layout

```text
AgnMonitor/
├── docker-compose.yml
├── nginx.conf
├── load_test_monitor.py
├── load_test_telegraf_simulator.py
├── data/
│   ├── config/             # Django project settings, ASGI/WSGI, Celery
│   ├── core_dashboard/     # Metrics, dashboards, alerts, consumers, tasks
│   ├── templates/          # Shared templates
│   ├── static/             # Static source files
│   ├── locale/             # i18n catalogs
│   └── telegraf.conf       # Sample Telegraf agent configuration
└── make_deploy.sh
```

## Security Notes

Do not commit runtime secrets or generated data:

- `data/.env`
- `data/.secret_key`
- `data/core_dashboard/.secret.key`
- `mariadb_data/`
- `valkey_data/`
- `backup/`
- `logs/`

For production deployments, set strong admin and database passwords, configure SMTP credentials through environment variables, and run with `DEBUG=False`.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
