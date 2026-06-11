import logging
from celery import shared_task
from django.utils import timezone
from .notifications import send_email_notification, send_webhook_notification

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def send_email_task(self, recipient, data):
    """
    이메일 알림 전송 태스크 (실패 시 최대 3회 재시도)
    """
    try:
        success = send_email_notification(recipient, data)
        if not success:
            raise Exception("Email notification failed")
    except Exception as exc:
        logger.warning(f"Retrying email task due to: {exc}")
        raise self.retry(exc=exc, countdown=60) # 1분 후 재시도

@shared_task(bind=True, max_retries=3)
def send_webhook_task(self, url, data):
    """
    웹훅 알림 전송 태스크 (실패 시 최대 3회 재시도)
    """
    try:
        success = send_webhook_notification(url, data)
        if not success:
            raise Exception("Webhook notification failed")
    except Exception as exc:
        logger.warning(f"Retrying webhook task due to: {exc}")
        raise self.retry(exc=exc, countdown=60)

@shared_task
def check_host_offline_task():
    """
    호스트 오프라인 체크 태스크 (Celery Beat에서 주기적으로 호출 예정)
    """
    from .views import check_host_offline
    # logger.debug("Starting scheduled host offline check...")
    check_host_offline()

@shared_task
def check_server_status_task():
    """
    서버 상태 체크 (API 및 Ping)
    사용자 요구사항: 10초 동안 5차례 못 받을 경우 알람
    """
    from .views import update_server_status_and_alert
    # logger.debug("Starting server status check (API/Ping)...")
    update_server_status_and_alert()

@shared_task
def data_retention_cleanup_task():
    """
    데이터 보관 정책에 따른 정리 태스크
    """
    from .views import run_data_retention_cleanup
    logger.info("Starting scheduled data retention cleanup...")
    run_data_retention_cleanup()

@shared_task
def aggregate_metrics_task():
    """
    메트릭 집계 태스크 (5분마다 실행)
    Phase 3-3: N+1 쿼리 문제 해결 - Python 메모리 집계 + bulk_create 사용
    - 기존: 호스트×소스×필드×agg_type 수만큼 get_or_create (수천 쿼리)
    - 개선: 전체 데이터 한 번에 읽기 → Python 집계 → bulk_create (2 쿼리)
    """
    from datetime import timedelta
    from .models import MonitoringMetric, MetricAggregate

    now = timezone.now()
    cutoff_time = now - timedelta(hours=1)

    try:
        # 1. 1시간 이상 된 메트릭 전체를 한 번에 조회 (쿼리 1회)
        old_metrics = list(MonitoringMetric.objects.filter(
            timestamp__lt=cutoff_time
        ).values('hostname', 'source', 'timestamp', 'data'))

        if not old_metrics:
            return

        # 2. Python에서 5분 버킷별로 집계 (DB 쿼리 없음)
        buckets = {}  # {(hostname, source, bucket_start): {field: [values]}}
        for m in old_metrics:
            ts = m['timestamp']
            # 5분 단위 버킷 시작 시각 계산
            bucket_start = ts.replace(second=0, microsecond=0) - timedelta(minutes=ts.minute % 5)
            bucket_end = bucket_start + timedelta(minutes=5)
            key = (m['hostname'], m['source'], bucket_start, bucket_end)

            fields = m['data'].get('fields', {}) if isinstance(m['data'], dict) else {}
            for field_key, field_value in fields.items():
                if isinstance(field_value, (int, float)):
                    if key not in buckets:
                        buckets[key] = {}
                    if field_key not in buckets[key]:
                        buckets[key][field_key] = []
                    buckets[key][field_key].append(field_value)

        # 3. 이미 집계된 버킷 조회 (중복 방지, 쿼리 1회)
        existing_keys = set(MetricAggregate.objects.filter(
            timestamp__lt=cutoff_time if hasattr(MetricAggregate, 'timestamp') else None,
            period_start__gte=cutoff_time - timedelta(hours=2)
        ).values_list('hostname', 'source', 'period_start', 'period_end', 'agg_type')) \
            if False else set()  # unique_together가 ignore_conflicts로 처리

        # 4. MetricAggregate 객체 생성 (메모리에서)
        aggregates = []
        for (hostname, source, bucket_start, bucket_end), field_data in buckets.items():
            for field_key, values in field_data.items():
                if not values:
                    continue
                avg_val = sum(values) / len(values)
                max_val = max(values)
                min_val = min(values)
                meta = {'field': field_key, 'count': len(values)}

                aggregates.append(MetricAggregate(
                    hostname=hostname, source=source,
                    period_start=bucket_start, period_end=bucket_end,
                    agg_type='avg', value=avg_val, data=meta
                ))
                aggregates.append(MetricAggregate(
                    hostname=hostname, source=source,
                    period_start=bucket_start, period_end=bucket_end,
                    agg_type='max', value=max_val, data=meta
                ))
                aggregates.append(MetricAggregate(
                    hostname=hostname, source=source,
                    period_start=bucket_start, period_end=bucket_end,
                    agg_type='min', value=min_val, data=meta
                ))

        # 5. 집계 결과 한 번에 저장 (unique_together로 중복 자동 처리)
        if aggregates:
            MetricAggregate.objects.bulk_create(aggregates, ignore_conflicts=True, batch_size=1000)

        # 6. 원본 데이터 삭제 (쿼리 1회)
        deleted_count, _ = MonitoringMetric.objects.filter(timestamp__lt=cutoff_time).delete()

        logger.info(f"Aggregated {len(aggregates)} records from {len(old_metrics)} metrics, deleted {deleted_count} old records")

    except Exception as e:
        logger.error(f"Metric aggregation task failed: {e}")
