import json
import logging
import subprocess
from datetime import timedelta
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from django.core.cache import cache
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from .models import (
    MonitoringMetric, AlertRule, AlertHistory,
    MetricMetadata, ManagedServer, DataCollectionConfig, HostGroup,
    MetricAggregate
)
from .tasks import send_email_task, send_webhook_task
from .notifications import send_notification_async

logger = logging.getLogger(__name__)

# ============================================================================
# Metric Sampling Helpers (성능 최적화: 메트릭 저장 50% 감소)
# ============================================================================

def get_metric_sampling_rates():
    """
    메트릭 소스별 샘플링 비율 조회
    같은 호스트/소스 조합은 항상 같은 결정 (해시 기반)

    Returns:
        dict: {source: sampling_rate} 예: {'cpu': 1.0, 'network': 0.5, 'custom_log': 0.1}
    """
    return {
        'cpu': 1.0,              # 100% 저장 (중요도 높음)
        'mem': 1.0,              # 100% 저장
        'disk': 1.0,             # 100% 저장
        'system': 1.0,           # 100% 저장
        'net': 0.5,              # 50% 샘플링 (중간 중요도)
        'diskio': 0.5,           # 50% 샘플링
        'processes': 0.5,        # 50% 샘플링
        'custom_logs': 0.1,      # 10% 샘플링 (낮은 중요도)
        'win_cpu': 1.0,          # 100% 저장
        'sql_server': 0.5,       # 50% 샘플링
    }

def should_save_metric(hostname, metric_source):
    """
    메트릭 저장 여부를 결정 (해시 기반 결정적 샘플링)

    같은 호스트/소스 조합은 항상 같은 결정을 하므로
    대시보드에서 일관성 있게 데이터를 볼 수 있음.

    Args:
        hostname: 호스트명
        metric_source: 메트릭 소스 (예: 'cpu', 'memory')

    Returns:
        bool: 저장할 경우 True, 미저장할 경우 False
    """
    import hashlib

    # 기본값 (설정되지 않은 메트릭은 50% 샘플링)
    sampling_rates = get_metric_sampling_rates()
    sample_rate = sampling_rates.get(metric_source, 0.5)

    # 100% 저장하면 조건 체크 불필요
    if sample_rate >= 1.0:
        return True

    # 해시 기반 샘플링 (같은 호스트/소스는 항상 같은 결과)
    hash_input = f"{hostname}:{metric_source}"
    hash_value = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)

    return (hash_value % 100) < (sample_rate * 100)

# ============================================================================
# AlertRule Caching Helpers (성능 최적화: 2초마다 80회 쿼리 → 12회/시간으로 감소)
# ============================================================================

def get_cached_alert_rules(metric_source=None, condition=None, is_active=True, cache_ttl=300):
    """
    AlertRule을 캐싱으로 조회. TTL은 5분(300초).
    변경 빈도가 낮아서 캐싱 효과가 매우 큼.

    Args:
        metric_source: 필터링할 메트릭 소스 (예: 'STATUS_API', 'STATUS_PING')
        condition: 필터링할 조건
        is_active: 활성 여부
        cache_ttl: 캐시 TTL (초)

    Returns:
        QuerySet or list of AlertRule objects
    """
    # 캐시 키 생성 (필터 조건 포함)
    cache_key = f"alert_rules:{metric_source}:{condition}:{is_active}"

    # 캐시 확인
    cached_rules = cache.get(cache_key)
    if cached_rules is not None:
        return cached_rules

    # DB 조회
    filters = {}
    if is_active is not None:
        filters['is_active'] = is_active
    if metric_source:
        filters['metric_source'] = metric_source
    if condition:
        filters['condition'] = condition

    rules = list(AlertRule.objects.filter(**filters).prefetch_related('host_group', 'host_group__hosts'))

    # 캐시 저장
    cache.set(cache_key, rules, cache_ttl)

    return rules

def invalidate_alert_rules_cache():
    """
    AlertRule 캐시 무효화 (규칙 생성/수정/삭제 시 호출)
    """
    # 모든 관련 캐시 키 삭제
    patterns = [
        'alert_rules:*',
        'active_alert_rules',  # check_alerts 함수의 캐시
    ]

    for pattern in patterns:
        # Django cache는 패턴 삭제를 직접 지원 안 하므로, 알려진 패턴만 삭제
        cache.delete_pattern(pattern) if hasattr(cache, 'delete_pattern') else None

    # 간단한 대안: 알려진 캐시 키들만 명시적으로 삭제
    for metric_source in ['STATUS_API', 'STATUS_PING', None]:
        for condition in ['status_check', 'host_down', None]:
            for is_active in [True, False, None]:
                cache.delete(f"alert_rules:{metric_source}:{condition}:{is_active}")

    # 일반 활성 규칙 캐시도 삭제
    cache.delete('active_alert_rules')

def update_server_status_and_alert():
    """
    Celery Beat 작업 (30초마다):
    1. API 상태 판정 (메트릭 수집 타임스탐프 기반)
    2. Ping 체크
    3. 알람 발송
    """
    now = timezone.now()
    servers = ManagedServer.objects.all()
    channel_layer = get_channel_layer()

    # API 및 Ping 규칙 조회 (캐싱)
    all_api_rules = get_cached_alert_rules(
        metric_source='STATUS_API',
        condition='status_check',
        is_active=True
    )

    all_ping_rules = get_cached_alert_rules(
        metric_source='STATUS_PING',
        condition='status_check',
        is_active=True
    )

    ping_required_hosts = set()
    for rule in all_ping_rules:
        if rule.target_type == 'all':
            ping_required_hosts.update(servers.values_list('hostname', flat=True))
            break
        elif rule.target_type == 'single' and rule.hostname:
            ping_required_hosts.add(rule.hostname)
        elif rule.target_type == 'group' and rule.host_group:
            ping_required_hosts.update(rule.host_group.hosts.values_list('hostname', flat=True))

    def matches_rule(rule, hostname):
        """규칙이 호스트와 일치하는지 확인"""
        if rule.target_type == 'all':
            return True
        elif rule.target_type == 'single':
            return rule.hostname == hostname
        elif rule.target_type == 'group' and rule.host_group:
            return rule.host_group.hosts.filter(hostname=hostname).exists()
        return False

    servers_to_update = []

    for server in servers:
        # 1. API 상태 판정 (임계치 기반)
        active_api_rule = next((r for r in all_api_rules if matches_rule(r, server.hostname)), None)

        new_api_status = 'normal'
        api_threshold = 30  # 기본 임계치
        if active_api_rule:
            api_threshold = active_api_rule.threshold if active_api_rule.threshold > 0 else 30

        reference_time_api = server.last_success_api_at or server.created_at
        if now > reference_time_api + timedelta(seconds=api_threshold):
            new_api_status = 'abnormal'
            if active_api_rule and can_retrigger_alert(active_api_rule, server.hostname):
                trigger_status_alert(server, "API", active_api_rule, _("{} seconds no data received").format(int(api_threshold)))

        # 2. Ping 체크 먼저 수행 (판정 전에 최신 결과 반영)
        if server.hostname in ping_required_hosts:
            target_ip = server.reachable_ip or server.last_detected_ip
            if target_ip and target_ip != 'Unknown':
                try:
                    res = subprocess.run(['ping', '-c', '1', '-W', '0.5', target_ip],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0:
                        server.last_success_ping_at = now
                    server.last_ping_at = now
                except Exception as e:
                    logger.error(f"Ping failed for {server.hostname}: {e}")

        # 3. Ping 상태 판정 (갱신된 last_success_ping_at 기준)
        active_ping_rule = next((r for r in all_ping_rules if matches_rule(r, server.hostname)), None)

        new_ping_status = 'normal'
        ping_threshold = 10  # 기본 임계치
        if active_ping_rule:
            ping_threshold = active_ping_rule.threshold if active_ping_rule.threshold > 0 else 10

        reference_time_ping = server.last_success_ping_at or server.created_at
        if now > reference_time_ping + timedelta(seconds=ping_threshold):
            new_ping_status = 'abnormal'
            if active_ping_rule and can_retrigger_alert(active_ping_rule, server.hostname):
                trigger_status_alert(server, "Ping", active_ping_rule, _("{} seconds no ping response").format(int(ping_threshold)))

        # 4. 상태 변경 사항 저장 및 WebSocket 알림
        if server.status_api != new_api_status or server.status_ping != new_ping_status:
            server.status_api = new_api_status
            server.status_ping = new_ping_status

        servers_to_update.append(server)

    # 벌크 업데이트
    if servers_to_update:
        ManagedServer.objects.bulk_update(
            servers_to_update,
            ['status_api', 'status_ping', 'last_ping_at', 'last_success_ping_at']
        )

        # WebSocket 알림
        if channel_layer:
            for server in servers_to_update:
                if server.status_api != 'normal' or server.status_ping != 'normal':
                    async_to_sync(channel_layer.group_send)(
                        "realtime_metrics",
                        {
                            "type": "server_status_update",
                            "hostname": server.hostname,
                            "status_api": server.status_api,
                            "status_ping": server.status_ping
                        }
                    )

    # 오프라인 체크
    check_host_offline()


def can_retrigger_alert(rule, hostname):
    """
    해당 규칙과 호스트에 대해 알람 재발송 가능 여부 확인.
    - 상태 체크(STATUS_API, STATUS_PING, host_down)의 경우:
      임계치(Threshold)와 냉각 기간(Cooldown) 중 더 긴 시간을 주기로 사용.
    - 일반 메트릭의 경우:
      설정된 냉각 기간(Cooldown)을 주기로 사용.
    """
    if not rule.is_active:
        return False

    last_history = AlertHistory.objects.filter(rule=rule, hostname=hostname).order_by('-timestamp').first()
    if not last_history:
        return True

    now = timezone.now()
    is_status_check = rule.metric_source in ['STATUS_API', 'STATUS_PING'] or rule.condition == 'host_down'

    cooldown_seconds = rule.cooldown_minutes * 60

    if is_status_check:
        # 상태 체크는 임계치(초) 자체가 하나의 주기이므로, 냉각 기간과 비교하여 더 큰 값을 주기로 채택
        threshold_seconds = rule.threshold if rule.threshold > 0 else 10
        interval_seconds = max(threshold_seconds, cooldown_seconds)
        # 최소값: 임계치를 존중하되 최소 1초
        if interval_seconds < 1: interval_seconds = 1
        interval = timedelta(seconds=interval_seconds)
    else:
        # 일반 메트릭은 냉각 기간만 따름 (0분일 경우 최소 1분 방어)
        effective_cooldown = rule.cooldown_minutes if rule.cooldown_minutes > 0 else 1
        interval = timedelta(minutes=effective_cooldown)

    if now >= last_history.timestamp + interval:
        return True

    return False
def trigger_status_alert(server, check_type, rule, message):
    """
    상태 이상 시 알람 발생 및 알림 전송
    """
    now = timezone.now()
    source = f"STATUS_{check_type}"

    # 규칙의 전역 최근 발생 시각 갱신
    rule.last_triggered_at = now
    rule.save(update_fields=['last_triggered_at'])

    # 알람 이력 생성 (호스트 정보 포함)
    AlertHistory.objects.create(
        rule=rule,
        hostname=server.hostname,
        value=0.0,
        severity=rule.severity
    )    
    alert_data = {
        "rule_name": rule.name,
        "hostname": server.hostname,
        "metric": source,
        "value": "OFFLINE" if check_type == "API" else "NO_RESPONSE",
        "threshold": f"{rule.threshold}s",
        "condition": "status_check",
        "severity": rule.severity,
        "message": message,
        "timestamp": timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')
    }
    # WebSocket 브로드캐스트
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            "realtime_metrics",
            {"type": "alert_notification", "alert": alert_data}
        )
    
    # 이메일/웹훅 전송
    send_notification_async(rule, alert_data)

def check_host_offline():
    """
    Checks for 'host_down' alert rules.
    Triggers an alert if no data has been received from a host for more than 30 seconds.
    """
    now = timezone.now()
    offline_rules = get_cached_alert_rules(condition='host_down', is_active=True)
    channel_layer = get_channel_layer()
    
    # We need to expand rules to individual hosts if they are 'group' or 'all'
    expanded_checks = []
    for rule in offline_rules:
        if rule.target_type == 'all':
            for s in ManagedServer.objects.all():
                expanded_checks.append((rule, s.hostname))
        elif rule.target_type == 'group' and rule.host_group:
            for s in rule.host_group.hosts.all():
                expanded_checks.append((rule, s.hostname))
        else:
            expanded_checks.append((rule, rule.hostname))

    for rule, hostname in expanded_checks:
        if not hostname: continue
        
        # 호스트별 냉각 기간 체크
        if not can_retrigger_alert(rule, hostname):
            continue
            
        last_meta = MetricMetadata.objects.filter(hostname=hostname).order_by('-last_seen').first()
        
        is_offline = False
        threshold_seconds = rule.threshold if rule.threshold > 0 else 30
        if not last_meta:
            if now > rule.created_at + timedelta(seconds=max(60, threshold_seconds)):
                is_offline = True
        else:
            if now > last_meta.last_seen + timedelta(seconds=threshold_seconds):
                is_offline = True
                
        if is_offline:
            AlertHistory.objects.create(
                rule=rule,
                hostname=hostname,
                value=0.0,
                severity=rule.severity
            )
            rule.last_triggered_at = now
            rule.save(update_fields=['last_triggered_at'])
            
            alert_data = {
                "rule_name": rule.name,
                "hostname": hostname,
                "metric": "HOST_STATUS",
                "value": "OFFLINE",
                "threshold": f"{threshold_seconds}s",
                "condition": "host_down",
                "severity": rule.severity,
                "timestamp": timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')
            }
            
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    "realtime_metrics",
                    {"type": "alert_notification", "alert": alert_data}
                )
            
            # Send notification asynchronously (using Threading as fallback for Celery)
            send_notification_async(rule, alert_data)

def run_data_retention_cleanup():
    """
    Cleans up old MonitoringMetric data based on retention policies.
    """
    now = timezone.now()
    three_days_ago = now - timedelta(days=3)
    
    managed_servers = ManagedServer.objects.all()
    managed_hostnames = set()
    
    for s in managed_servers:
        managed_hostnames.add(s.hostname)
        cutoff = now - timedelta(days=s.retention_days)
        MonitoringMetric.objects.filter(hostname=s.hostname, timestamp__lt=cutoff).delete()
    
    all_hosts = set(MonitoringMetric.objects.values_list('hostname', flat=True).distinct())
    for hostname in all_hosts:
        if not hostname or hostname in managed_hostnames:
            continue
        MonitoringMetric.objects.filter(hostname=hostname, timestamp__lt=three_days_ago).delete()
    
    logger.info("Data retention cleanup completed.")

def get_all_hosts():
    managed = ManagedServer.objects.all()
    results = []
    for s in managed:
        results.append({
            'hostname': s.hostname,
            'nickname': s.nickname or s.hostname
        })
    # Sort by nickname (falling back to hostname) case-insensitive
    results.sort(key=lambda x: x['nickname'].lower())
    return results

@login_required
def index(request):
    return render(request, 'core_dashboard/index.html', {'all_hosts': get_all_hosts()})

@login_required
def server_dashboard(request, hostname):
    managed = ManagedServer.objects.filter(hostname=hostname).first()
    target_nickname = managed.nickname if managed and managed.nickname else hostname
    return render(request, 'core_dashboard/index.html', {
        'target_host': hostname,
        'target_nickname': target_nickname,
        'all_hosts': get_all_hosts()
    })

@login_required
def alerts_page(request):
    return render(request, 'core_dashboard/alerts.html', {'all_hosts': get_all_hosts()})

@login_required
def server_management(request):
    return render(request, 'core_dashboard/manage.html', {'all_hosts': get_all_hosts()})

def check_alerts(metrics):
    """
    Checks incoming metrics against active alert rules.
    Optimized: Cache active rules for 60 seconds to reduce DB load and CPU usage.
    Phase 3-3: Pre-index rules by metric_source for O(1) lookup instead of O(n) scan.
    """
    cache_key = "active_alert_rules"
    active_rules = cache.get(cache_key)

    if active_rules is None:
        active_rules = list(AlertRule.objects.filter(is_active=True).prefetch_related('host_group', 'host_group__hosts'))
        cache.set(cache_key, active_rules, 60)

    if not active_rules:
        return

    # Phase 3-3: Pre-index rules by source for O(1) lookup (avoid O(rules) scan per metric)
    rules_by_source = {}
    group_to_hosts = {}
    for r in active_rules:
        rules_by_source.setdefault(r.metric_source, []).append(r)
        if r.target_type == 'group' and r.host_group:
            if r.host_group_id not in group_to_hosts:
                group_to_hosts[r.host_group_id] = set(r.host_group.hosts.values_list('hostname', flat=True))

    channel_layer = get_channel_layer()
    now = timezone.now()

    triggered_alerts = []
    alert_histories = []
    rules_to_update = []
    # 같은 배치 내에서 이미 트리거된 (rule_id, hostname) 쌍 추적 — bulk_create 전 DB에 반영되지 않으므로 중복 발송 방지
    triggered_in_batch = set()

    for item in metrics:
        source = item.get('name')
        tags = item.get('tags', {})
        hostname = tags.get('host')
        fields = item.get('fields', {})

        # Phase 3-3: O(1) source lookup instead of O(all_rules) scan
        source_rules = rules_by_source.get(source, [])
        relevant_rules = []
        for r in source_rules:
            is_match = False
            if r.target_type == 'all':
                is_match = True
            elif r.target_type == 'single' and r.hostname == hostname:
                is_match = True
            elif r.target_type == 'group' and r.host_group_id:
                if hostname in group_to_hosts.get(r.host_group_id, set()):
                    is_match = True

            if is_match:
                relevant_rules.append(r)

        for rule in relevant_rules:
            # 같은 배치 내 중복 트리거 방지 (bulk_create 전이므로 DB 냉각기간 체크가 통과될 수 있음)
            batch_key = (rule.id, hostname)
            if batch_key in triggered_in_batch:
                continue

            # Cooldown check (Host-specific)
            if not can_retrigger_alert(rule, hostname):
                continue

            # Tag filter check
            if rule.filter_key and rule.filter_value:
                if str(tags.get(rule.filter_key)) != str(rule.filter_value):
                    continue

            triggered = False
            triggered_value = None

            if source == 'custom_logs':
                log_line = fields.get('value', '')
                if not rule.log_keyword or rule.log_keyword.lower() in log_line.lower():
                    triggered = True
                    triggered_value = 1.0
            else:
                val = fields.get(rule.metric_field)
                if val is None:
                    continue

                if source == 'cpu' and rule.metric_field == 'usage_idle':
                    val = 100 - val
                
                if rule.condition == 'gt' and val > rule.threshold:
                    triggered = True
                elif rule.condition == 'lt' and val < rule.threshold:
                    triggered = True
                elif rule.condition == 'eq' and val == rule.threshold:
                    triggered = True
                
                triggered_value = val

            if triggered:
                triggered_in_batch.add(batch_key)
                alert_histories.append(AlertHistory(
                    rule=rule,
                    hostname=hostname,
                    value=triggered_value or 0.0,
                    severity=rule.severity
                ))
                rule.last_triggered_at = now
                rules_to_update.append(rule)
                
                alert_data = {
                    "rule_name": rule.name,
                    "hostname": hostname,
                    "metric": f"{rule.metric_source}.{rule.metric_field}",
                    "value": triggered_value,
                    "threshold": rule.threshold,
                    "keyword": rule.log_keyword,
                    "condition": rule.condition,
                    "severity": rule.severity,
                    "timestamp": timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')
                }
                triggered_alerts.append(alert_data)
                
                # Send notification asynchronously (using Threading as fallback for Celery)
                send_notification_async(rule, alert_data)

    if alert_histories:
        AlertHistory.objects.bulk_create(alert_histories)
    
    if rules_to_update:
        # Use a set to avoid updating the same rule multiple times in one batch
        unique_rules = {r.id: r for r in rules_to_update}.values()
        AlertRule.objects.bulk_update(unique_rules, ['last_triggered_at'])

    if triggered_alerts and channel_layer:
        # Broadcast all alerts in one go or sequentially but after DB ops
        for alert in triggered_alerts:
            async_to_sync(channel_layer.group_send)(
                "realtime_metrics",
                {"type": "alert_notification", "alert": alert}
            )

@csrf_exempt
@require_POST
def collect_metrics(request):
    try:
        now = timezone.now()
        payload = json.loads(request.body)
        client_ip = request.META.get('HTTP_X_FORWARDED_FOR')
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()
        else:
            client_ip = request.META.get('REMOTE_ADDR')

        metrics_list = []
        if isinstance(payload, list):
            metrics_list = payload
        elif isinstance(payload, dict):
            metrics_list = payload.get('metrics', [payload])
        else:
            return JsonResponse({"status": "error", "message": "Invalid format"}, status=400)

        if not metrics_list:
            return JsonResponse({"status": "success", "count": 0})

        # 1. Pre-fetch hostnames and sources to minimize DB queries
        hostnames = set()
        host_source_pairs = set()
        for item in metrics_list:
            if not isinstance(item, dict): continue
            tags = item.get('tags', {})
            hostname = tags.get('host', 'unknown')
            source = item.get('name', 'unknown')
            hostnames.add(hostname)
            host_source_pairs.add((hostname, source))

        # Pre-fetch ManagedServers
        servers_qs = ManagedServer.objects.filter(hostname__in=hostnames)
        servers_cache = {s.hostname: s for s in servers_qs}
        
        # Pre-fetch DataCollectionConfigs
        common_configs = DataCollectionConfig.objects.filter(server=None)
        common_rules_by_type = {}
        for cc in common_configs:
            if cc.config and 'sources' in cc.config:
                common_rules_by_type[cc.config_type] = cc.config['sources']
        
        server_configs_qs = DataCollectionConfig.objects.filter(server__in=servers_qs)
        server_configs_lookup = {sc.server_id: sc for sc in server_configs_qs}
        
        server_configs_cache_map = {}
        for s in servers_qs:
            cfg = server_configs_lookup.get(s.id)
            if cfg and cfg.config and cfg.config.get('sources'):
                server_configs_cache_map[s.hostname] = cfg.config['sources']

        def get_storage_rules(hostname):
            # 1. 개별 서버 규칙 가져오기
            individual_rules = server_configs_cache_map.get(hostname, [])
            
            # 2. 공통 규칙 가져오기 (전체 공통 규칙 통합)
            common_rules = []
            for r_list in common_rules_by_type.values():
                common_rules.extend(r_list)
            
            # 3. 통합 (중복 제거 로직은 생략하거나 간단히 처리 - 동일 규칙이 두 번 적용되어도 필터링 단계에서 걸러짐)
            # 사용자의 편의를 위해 개별 서버 규칙이 설정되어 있어도 공통 규칙이 함께 작동하도록 병합
            return individual_rules + common_rules

        # Pre-fetch MetricMetadata
        # Note: hostname__in and source__in might fetch slightly more but is way faster than individual queries
        meta_qs = MetricMetadata.objects.filter(hostname__in=hostnames, source__in=[p[1] for p in host_source_pairs])
        meta_cache = {(m.hostname, m.source): m for m in meta_qs}

        monitoring_objs = []
        updated_hosts = set()
        meta_to_save = []
        
        for item in metrics_list:
            if not isinstance(item, dict): continue
                
            source = item.get('name', 'unknown')
            tags = item.get('tags', {})
            hostname = tags.get('host', 'unknown')
            fields = item.get('fields', {})

            # IP Update (Optimized: only once per host per request)
            if hostname not in updated_hosts:
                detected_ip = fields.get('value') if source == 'public_ip' else tags.get('ip') or tags.get('internal_ip')
                srv = servers_cache.get(hostname)
                if srv and (detected_ip or client_ip):
                    changed_srv = False
                    if detected_ip and srv.last_detected_ip != detected_ip:
                        srv.last_detected_ip = detected_ip
                        changed_srv = True
                    if client_ip and srv.reachable_ip != client_ip:
                        srv.reachable_ip = client_ip
                        changed_srv = True
                    
                    if changed_srv:
                        # We'll bulk update later
                        pass
                    updated_hosts.add(hostname)

            # Metadata Discovery
            try:
                meta_obj = meta_cache.get((hostname, source))
                if not meta_obj:
                    # DB에 없을 경우 생성 (get_or_create 효과)
                    meta_obj, created = MetricMetadata.objects.get_or_create(
                        hostname=hostname, source=source, 
                        defaults={'metadata': {}, 'last_seen': now}
                    )
                    meta_cache[(hostname, source)] = meta_obj
                
                # 시각 갱신 및 큐 추가
                meta_obj.last_seen = now
                if meta_obj not in meta_to_save:
                    meta_to_save.append(meta_obj)
                
                current_meta = meta_obj.metadata
                changed_meta = False
                
                existing_fields = set(current_meta.get('fields', []))
                new_fields = set(fields.keys())
                if not new_fields.issubset(existing_fields):
                    current_meta['fields'] = sorted(list(existing_fields | new_fields))
                    changed_meta = True

                if 'latest_values' not in current_meta:
                    current_meta['latest_values'] = {}
                for f_key, f_val in fields.items():
                    if isinstance(f_val, (int, float)):
                        current_meta['latest_values'][f_key] = f_val
                        changed_meta = True

                # 디스크 계열: 파티션(path/instance)별 최신값을 영구 보관
                # (latest_values는 source당 1세트라 파티션 구분 불가 → partitions 딕셔너리로 분리)
                if source in ('disk', 'win_disk', 'win_logicaldisk'):
                    part_key = (tags.get('path') or tags.get('instance')
                                or tags.get('device') or tags.get('volume'))
                    if part_key:
                        if 'partitions' not in current_meta:
                            current_meta['partitions'] = {}
                        current_meta['partitions'][part_key] = {
                            'device': tags.get('device', ''),
                            'fstype': tags.get('fstype', ''),
                            'values': {k: v for k, v in fields.items() if isinstance(v, (int, float))},
                            'seen': now.isoformat(),  # 마지막 수신 시각 (stale 파티션 정리용)
                        }
                        changed_meta = True

                        # stale 파티션 영구 제거: 일정 시간 이상 수신 없는 파티션을 metadata에서 삭제
                        # (예: VM에서 ISO/CD 드라이브를 꺼내면 해당 파티션이 더 이상 안 들어옴)
                        from django.utils.dateparse import parse_datetime
                        stale_cutoff = now - timedelta(seconds=900)  # 15분
                        for pk in list(current_meta['partitions'].keys()):
                            p_seen = current_meta['partitions'][pk].get('seen')
                            if not p_seen:
                                # 구버전 데이터(seen 없음)는 이번에 seen을 채워 넣어 기준점 부여
                                current_meta['partitions'][pk]['seen'] = now.isoformat()
                                continue
                            sdt = parse_datetime(p_seen)
                            if sdt and sdt < stale_cutoff:
                                del current_meta['partitions'][pk]

                if 'tags' not in current_meta:
                    current_meta['tags'] = {}
                existing_tags = current_meta['tags']
                for t_key, t_val in tags.items():
                    if t_key == 'host': continue
                    if t_key not in existing_tags:
                        existing_tags[t_key] = [t_val]
                        changed_meta = True
                    elif t_val not in existing_tags[t_key]:
                        existing_tags[t_key].append(t_val)
                        existing_tags[t_key].sort()
                        changed_meta = True
                
                if client_ip and current_meta.get('reachable_ip') != client_ip:
                    current_meta['reachable_ip'] = client_ip
                    changed_meta = True
                
                detected_ip_discovery = fields.get('value') if source == 'public_ip' else tags.get('ip') or tags.get('internal_ip')
                if detected_ip_discovery and current_meta.get('public_ip') != detected_ip_discovery:
                    current_meta['public_ip'] = detected_ip_discovery
                    changed_meta = True
                
                if changed_meta:
                    meta_obj.metadata = current_meta
            except Exception as me:
                logger.error(f"Metadata update failed: {me}")

            # Storage Policy
            rules = get_storage_rules(hostname)
            matched_fields = set()
            store_all_fields = False
            
            for r in rules:
                rule_type = r.get('type')
                if rule_type == 'all' or rule_type == source:
                    f_key = r.get('filter_key')
                    f_val = r.get('filter_value')
                    
                    if f_key and str(tags.get(f_key)) != str(f_val):
                        continue

                    target_field = r.get('field')
                    if not target_field:
                        store_all_fields = True
                        break
                    elif target_field in fields:
                        matched_fields.add(target_field)
            
            # 저장 정책 및 샘플링 체크 (성능 최적화)
            should_store = store_all_fields or matched_fields

            # 저장 정책에 맞으면 샘플링 적용
            if should_store and not should_save_metric(hostname, source):
                # 샘플링에서 제외됨 - DB에는 저장하지 않음
                should_store = False

            if should_store:
                storage_data = item.copy()
                if not store_all_fields:
                    storage_data['fields'] = {k: v for k, v in fields.items() if k in matched_fields}

                monitoring_objs.append(MonitoringMetric(
                    source=source,
                    data=storage_data,
                    hostname=hostname,
                    level="INFO"
                ))
            
        # 2. Bulk Database Operations (Phase 3-2: batch_size optimization)
        if monitoring_objs:
            MonitoringMetric.objects.bulk_create(monitoring_objs, batch_size=1000)

        # Phase 3-3: Combine two ManagedServer bulk_update calls into one
        if updated_hosts:
            servers_to_update = [s for s in servers_cache.values() if s.hostname in updated_hosts]
            if servers_to_update:
                for srv in servers_to_update:
                    srv.last_success_api_at = now
                ManagedServer.objects.bulk_update(
                    servers_to_update,
                    ['last_detected_ip', 'reachable_ip', 'last_success_api_at']
                )

        if meta_to_save:
            unique_metas = {m.id: m for m in meta_to_save if m.id}.values()
            new_metas = [m for m in meta_to_save if not m.id]

            if new_metas:
                MetricMetadata.objects.bulk_create(new_metas, ignore_conflicts=True, batch_size=1000)
            if unique_metas:
                MetricMetadata.objects.bulk_update(unique_metas, ['metadata', 'last_seen'])

        # 4. Cache Latest Metrics to Valkey (Phase 2+3-2: batch optimization)
        try:
            cache_data = {}
            for item in metrics_list:
                if not isinstance(item, dict): continue
                source = item.get('name', 'unknown')
                tags = item.get('tags', {})
                hostname = tags.get('host', 'unknown')
                fields = item.get('fields', {})

                # Batch cache keys (Phase 3-2: set_many instead of individual set calls)
                cache_key = f"latest_metrics:{hostname}:{source}"
                cache_data[cache_key] = {
                    'source': source,
                    'fields': fields,
                    'tags': tags,
                    'timestamp': now.isoformat()
                }

            # Phase 3-2: Batch set all caches at once (more efficient than individual set calls)
            if cache_data:
                cache.set_many(cache_data, 120)  # 2분 TTL for all

            # Also invalidate bulk cache to force refresh
            cache.delete('latest_metrics_all')
        except Exception as ce:
            logger.warning(f"Latest metrics caching failed: {ce}")

        # 5. Alerts and Broadcast
        try:
            check_alerts(metrics_list)
            channel_layer = get_channel_layer()
            if channel_layer:
                # Phase 3: Send metrics to host-specific groups (not "realtime_metrics")
                # Group by hostname to reduce broadcast overhead
                metrics_by_host = {}
                for metric in metrics_list:
                    hostname = metric.get('tags', {}).get('host', 'unknown')
                    if hostname not in metrics_by_host:
                        metrics_by_host[hostname] = []
                    metrics_by_host[hostname].append(metric)

                # Send each host's metrics only to subscribed clients
                for hostname, host_metrics in metrics_by_host.items():
                    async_to_sync(channel_layer.group_send)(
                        f"host_{hostname}",
                        {"type": "broadcast_metrics", "metrics": host_metrics}
                    )
        except Exception as ae:
            logger.error(f"Broadcast failed: {ae}")

        return JsonResponse({"status": "success", "count": len(monitoring_objs)})
    except Exception as e:
        logger.error(f"Collect metrics error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return JsonResponse({"status": "error", "message": str(e)}, status=500)
    except Exception as e:
        logger.error(f"Collect metrics error: {e}")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

@login_required
def data_viewer_page(request):
    return render(request, 'core_dashboard/data_viewer.html', {'all_hosts': get_all_hosts()})

@login_required
def data_collection_page(request):
    all_servers = list(ManagedServer.objects.all())
    all_servers.sort(key=lambda x: (x.nickname or x.hostname).lower())
    linux_common, _ = DataCollectionConfig.objects.get_or_create(server=None, config_type="linux")
    win_common, _ = DataCollectionConfig.objects.get_or_create(server=None, config_type="windows")
    return render(request, "core_dashboard/data_collection.html", {
        "all_hosts": get_all_hosts(), 
        "all_servers": all_servers, 
        "linux_common": linux_common, 
        "win_common": win_common
    })

@csrf_exempt
@login_required
@require_POST
def update_data_config(request):
    try:
        data = json.loads(request.body)
        server_id = data.get("server_id")
        config_type = data.get("config_type", "custom")
        config_data = data.get("config", {})
        if server_id:
            server = ManagedServer.objects.get(id=server_id)
            obj, _ = DataCollectionConfig.objects.get_or_create(server=server, config_type="custom")
        else:
            obj, _ = DataCollectionConfig.objects.get_or_create(server=None, config_type=config_type)
        obj.config = config_data
        obj.save()
        return JsonResponse({"status": "success"})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)

# ============================================================================
# Server Status Overview (서버 상태 일괄 조회 — 부하 최소화 스냅샷)
# ============================================================================

# 윈도우 메트릭 소스 접두사. 이 소스가 하나라도 있으면 윈도우로 판별.
WINDOWS_SOURCE_PREFIXES = ('win_', 'win', 'sql_server')

# 디스크 사용량 모니터링에서 제외할 가상/임시 파일시스템 (노이즈 제거)
VIRTUAL_FSTYPES = {
    'tmpfs', 'devtmpfs', 'devfs', 'overlay', 'squashfs', 'aufs',
    'proc', 'sysfs', 'cgroup', 'cgroup2', 'mqueue', 'debugfs',
    'tracefs', 'securityfs', 'pstore', 'bpf', 'configfs', 'ramfs',
    'autofs', 'hugetlbfs', 'fusectl', 'binfmt_misc', 'nsfs',
}
VIRTUAL_MOUNT_PREFIXES = ('/dev', '/run', '/sys', '/proc', '/tmp/containerd')

# 디스크 파티션이 이 시간(초) 이상 수신되지 않으면 stale로 보고 표시에서 제외한다.
# (예: VM에서 ISO/CD 드라이브를 꺼내면 해당 파티션 메트릭이 더 이상 들어오지 않음)
STALE_PARTITION_SECONDS = 600  # 10분


def _to_float(val):
    """숫자 변환 시도. 실패하면 None."""
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _human_bytes(num):
    """바이트를 사람이 읽기 쉬운 단위로 변환."""
    f = _to_float(num)
    if f is None:
        return None
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if abs(f) < 1024.0:
            return f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{f:.1f} EB"


def build_status_overview():
    """
    각 서버의 최신 스냅샷(CPU/RAM/파티션)을 구성한다.

    부하 최소화 전략:
    - MonitoringMetric 테이블을 새로 쿼리하지 않는다.
    - 수집 시점에 이미 채워진 MetricMetadata.metadata(fields/latest_values/tags)와
      Valkey 캐시(latest_metrics:{host}:{source})만 읽는다.
    - 따라서 서버당 추가 DB 쓰기/무거운 조회가 없다.

    Returns:
        {'linux': [...], 'windows': [...], 'collected_at': iso}
    """
    now = timezone.now()

    managed = list(ManagedServer.objects.all())
    nickname_map = {s.hostname: (s.nickname or s.hostname) for s in managed}
    last_seen_map = {}

    # 모든 메타데이터를 1회 조회 (서버당 N쿼리 회피)
    metas = MetricMetadata.objects.all()
    # {hostname: {source: metadata_dict}}
    host_sources = {}
    for m in metas:
        host_sources.setdefault(m.hostname, {})[m.source] = m.metadata or {}
        prev = last_seen_map.get(m.hostname)
        if prev is None or m.last_seen > prev:
            last_seen_map[m.hostname] = m.last_seen

    # 관리 서버 + 메타데이터가 있는 모든 호스트 통합
    all_hostnames = set(nickname_map) | set(host_sources)

    linux_rows = []
    windows_rows = []

    for hostname in sorted(all_hostnames, key=lambda h: (nickname_map.get(h, h) or h).lower()):
        sources = host_sources.get(hostname, {})

        # OS 판별: win_* / sql_server 소스가 있으면 윈도우
        is_windows = any(
            src.startswith(WINDOWS_SOURCE_PREFIXES) for src in sources
        )

        snapshot = _build_host_snapshot(hostname, sources, is_windows)
        snapshot['hostname'] = hostname
        snapshot['nickname'] = nickname_map.get(hostname, hostname)

        last_seen = last_seen_map.get(hostname)
        snapshot['last_seen'] = timezone.localtime(last_seen).strftime('%Y-%m-%d %H:%M:%S') if last_seen else None
        # 60초 이내 수신이면 온라인으로 간주
        snapshot['online'] = bool(last_seen and now - last_seen < timedelta(seconds=60))

        if is_windows:
            windows_rows.append(snapshot)
        else:
            linux_rows.append(snapshot)

    return {
        'linux': linux_rows,
        'windows': windows_rows,
        'collected_at': timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _build_host_snapshot(hostname, sources, is_windows):
    """
    단일 호스트의 CPU/RAM/디스크 스냅샷을 MetricMetadata에서 구성.
    캐시 비의존(영구 저장된 metadata만 사용) → 멀티워커 안전, 추가 DB 부하 없음.
    """
    snap = {'cpu_percent': None, 'mem_percent': None, 'mem_used': None,
            'mem_total': None, 'mem_available': None, 'partitions': []}

    # ── CPU ──
    # 표준 cpu(usage_idle) 우선, 없으면 win_cpu(Percent_Processor_Time) fallback.
    # (윈도우라도 telegraf 설정에 따라 표준 cpu를 쓰는 경우가 있음)
    cpu_lv = sources.get('cpu', {}).get('latest_values', {})
    idle = _to_float(cpu_lv.get('usage_idle'))
    if idle is not None:
        snap['cpu_percent'] = 100.0 - idle
    else:
        win_cpu_lv = sources.get('win_cpu', {}).get('latest_values', {})
        snap['cpu_percent'] = _to_float(win_cpu_lv.get('Percent_Processor_Time'))

    # ── RAM ──
    # 표준 mem(used_percent/used/total) 우선 — 윈도우도 inputs.mem 플러그인을 쓰면 동일.
    # 없을 때만 win_mem(perf_counters) fallback.
    mem_lv = sources.get('mem', {}).get('latest_values', {})
    if mem_lv:
        snap['mem_percent'] = _to_float(mem_lv.get('used_percent'))
        snap['mem_used'] = _human_bytes(mem_lv.get('used'))
        snap['mem_total'] = _human_bytes(mem_lv.get('total'))
    else:
        win_mem_lv = sources.get('win_mem', {}).get('latest_values', {})
        snap['mem_percent'] = _to_float(win_mem_lv.get('Percent_Committed_Bytes_In_Use'))
        avail = _to_float(win_mem_lv.get('Available_Bytes'))
        if avail is not None:
            snap['mem_available'] = _human_bytes(avail)

    # ── 디스크 파티션 ──
    # 표준 disk 우선, 없으면 win_disk/win_logicaldisk.
    if sources.get('disk', {}).get('partitions'):
        _collect_partitions(sources, snap, windows=False)
    else:
        _collect_partitions(sources, snap, windows=True)

    if snap['cpu_percent'] is not None:
        snap['cpu_percent'] = round(snap['cpu_percent'], 1)
    if snap['mem_percent'] is not None:
        snap['mem_percent'] = round(snap['mem_percent'], 1)
    return snap


def _collect_partitions(sources, snap, windows):
    """
    디스크 파티션 수집. collect_metrics()가 metadata['partitions']에
    파티션(path/instance)별 최신값을 영구 저장하므로 이를 그대로 읽는다.
    """
    from django.utils.dateparse import parse_datetime
    now = timezone.now()

    disk_sources = ('win_disk', 'win_logicaldisk') if windows else ('disk',)
    for src in disk_sources:
        partitions = sources.get(src, {}).get('partitions', {})
        for mount, info in partitions.items():
            # stale 파티션 제외: 마지막 수신이 임계 시간보다 오래되면 건너뜀
            # (VM에서 ISO/CD 드라이브를 꺼내면 해당 파티션이 더 이상 수신되지 않음)
            seen_raw = info.get('seen')
            if seen_raw:
                seen_dt = parse_datetime(seen_raw)
                if seen_dt and (now - seen_dt).total_seconds() > STALE_PARTITION_SECONDS:
                    continue

            vals = info.get('values', {})
            fstype = (info.get('fstype') or '').lower()
            # NTFS 등 윈도우 디스크는 표준 disk 소스로 와도 가상FS 필터 대상 아님
            is_win_disk = windows or fstype in ('ntfs', 'fat32', 'exfat', 'refs')
            if not is_win_disk:
                # 가상/임시 파일시스템 노이즈 제외 (리눅스 한정)
                if fstype in VIRTUAL_FSTYPES:
                    continue
                if mount.startswith(VIRTUAL_MOUNT_PREFIXES):
                    continue
            # 윈도우 경로 표기 정리: "\C:" → "C:"
            display_mount = mount.lstrip('\\') if (mount.endswith(':') or ':' in mount) else mount
            if windows:
                free_pct = _to_float(vals.get('Percent_Free_Space'))
                used_pct = _to_float(vals.get('Percent_Disk_Used'))
                if used_pct is None and free_pct is not None:
                    used_pct = round(100.0 - free_pct, 1)
                free_mb = _to_float(vals.get('Free_Megabytes'))
                snap['partitions'].append({
                    'mount': display_mount,
                    'device': info.get('device', ''),
                    'fstype': info.get('fstype', ''),
                    'used_percent': used_pct,
                    'used': None,
                    'total': None,
                    'free': _human_bytes(free_mb * 1024 * 1024) if free_mb is not None else None,
                })
            else:
                up = _to_float(vals.get('used_percent'))
                snap['partitions'].append({
                    'mount': display_mount,
                    'device': info.get('device', ''),
                    'fstype': info.get('fstype', ''),
                    'used_percent': round(up, 1) if up is not None else None,
                    'used': _human_bytes(vals.get('used')),
                    'total': _human_bytes(vals.get('total')),
                    'free': _human_bytes(vals.get('free')),
                })
    snap['partitions'].sort(key=lambda p: p['mount'])


@login_required
def status_overview_page(request):
    """서버 상태 일괄 조회 페이지 (진입 시점 스냅샷, 실시간 갱신 없음)."""
    data = build_status_overview()
    return render(request, 'core_dashboard/status_overview.html', {
        'all_hosts': get_all_hosts(),
        'linux_servers': data['linux'],
        'windows_servers': data['windows'],
        'collected_at': data['collected_at'],
    })


@login_required
def status_overview_download(request):
    """서버 상태 스냅샷을 CSV(UTF-8 BOM)로 다운로드. os 파라미터로 linux/windows 구분."""
    import csv
    from django.http import HttpResponse

    os_type = request.GET.get('os', 'linux')
    data = build_status_overview()
    rows = data['windows'] if os_type == 'windows' else data['linux']

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    filename = f"server_status_{os_type}_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        _('Nickname'), _('Hostname'), _('Online'), _('Last Seen'),
        _('CPU (%)'), _('RAM (%)'), _('RAM Used'), _('RAM Total'),
        _('Mount'), _('Device'), _('FS Type'), _('Disk (%)'), _('Disk Used'), _('Disk Total'), _('Disk Free'),
    ])

    for s in rows:
        base = [
            s.get('nickname', ''), s.get('hostname', ''),
            'O' if s.get('online') else 'X', s.get('last_seen') or '',
            s.get('cpu_percent') if s.get('cpu_percent') is not None else '',
            s.get('mem_percent') if s.get('mem_percent') is not None else '',
            s.get('mem_used') or s.get('mem_available') or '', s.get('mem_total') or '',
        ]
        partitions = s.get('partitions') or []
        if not partitions:
            writer.writerow(base + ['', '', '', '', '', '', ''])
        else:
            for p in partitions:
                writer.writerow(base + [
                    p.get('mount', ''), p.get('device', ''), p.get('fstype', ''),
                    p.get('used_percent') if p.get('used_percent') is not None else '',
                    p.get('used') or '', p.get('total') or '', p.get('free') or '',
                ])

    return response


@login_required
def get_data_config(request, server_id=None):
    config_type = request.GET.get('type', 'custom')
    try:
        if server_id:
            server = ManagedServer.objects.get(id=server_id)
            obj = DataCollectionConfig.objects.filter(server=server, config_type='custom').first()
        else:
            obj = DataCollectionConfig.objects.filter(server=None, config_type=config_type).first()
        config = obj.config if obj else {}
        return JsonResponse({"status": "success", "config": config})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)
