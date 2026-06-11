from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

class MonitoringMetric(models.Model):
    """
    통합된 메트릭 저장 모델.
    기존 MonitoringMetric(JSON 원본)과 OptimizedMetric(필드별 분리)을 하나로 통합하여
    디비 용량 절약 및 관리 복잡도를 낮춤.
    """
    hostname = models.CharField(_('호스트명'), max_length=100, db_index=True, default='unknown')
    source = models.CharField(_('소스'), max_length=100, db_index=True)
    timestamp = models.DateTimeField(_('타임스탬프'), auto_now_add=True, db_index=True)
    
    # 원본 데이터 통합 저장 (fields, tags 포함)
    data = models.JSONField(_('데이터')) 
    
    level = models.CharField(_('레벨'), max_length=20, default='INFO')

    class Meta:
        verbose_name = _('모니터링 메트릭')
        verbose_name_plural = _('모니터링 메트릭 목록')
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['hostname', 'source', 'timestamp']),
        ]

    def __str__(self):
        return f"[{self.hostname}] {self.source} at {self.timestamp}"

class DashboardPanel(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name=_('사용자'))
    title = models.CharField(_('패널 제목'), max_length=100)
    host = models.CharField(_('호스트'), max_length=100, db_index=True)
    parent_host = models.CharField(_('상위 호스트'), max_length=100, blank=True, default='')
    metric_source = models.CharField(_('메트릭 소스'), max_length=50)
    metric_field = models.CharField(_('메트릭 필드'), max_length=100)
    chart_type = models.CharField(_('차트 타입'), max_length=20, default='text') # 'text', 'line'
    
    filter_key = models.CharField(_('필터 키'), max_length=50, blank=True, default='')
    filter_value = models.CharField(_('필터 값'), max_length=100, blank=True, default='')
    log_filter = models.CharField(_('로그 필터'), max_length=100, blank=True, default='')
    memo_content = models.TextField(_('메모 내용'), blank=True, default='')
    
    x = models.IntegerField(default=0)
    y = models.IntegerField(default=0)
    w = models.IntegerField(default=2)
    h = models.IntegerField(default=2)

    class Meta:
        verbose_name = _('대시보드 패널')
        verbose_name_plural = _('대시보드 패널 목록')
        ordering = ['y', 'x']

class ManagedServer(models.Model):
    hostname = models.CharField(_('호스트명'), max_length=100, unique=True)
    nickname = models.CharField(_('닉네임'), max_length=100, blank=True, default='')
    last_detected_ip = models.GenericIPAddressField(_('최근 감지 IP'), null=True, blank=True)
    reachable_ip = models.GenericIPAddressField(_('접속 IP'), null=True, blank=True)
    retention_days = models.IntegerField(_('데이터 보존 기간(일)'), default=14)
    
    # 서버 상태 정보
    status_api = models.CharField(_('API 상태'), max_length=20, default='normal') # 'normal', 'abnormal'
    status_ping = models.CharField(_('Ping 상태'), max_length=20, default='normal') # 'normal', 'abnormal'
    last_ping_at = models.DateTimeField(_('최근 핑 시도 시각'), null=True, blank=True)
    last_success_ping_at = models.DateTimeField(_('최근 핑 성공 시각'), null=True, blank=True)
    last_success_api_at = models.DateTimeField(_('최근 API 성공 시각'), null=True, blank=True)
    
    created_at = models.DateTimeField(_('등록일'), auto_now_add=True)

    class Meta:
        verbose_name = _('관리 서버')
        verbose_name_plural = _('관리 서버 목록')

class HostGroup(models.Model):
    name = models.CharField(_('그룹 이름'), max_length=100, unique=True)
    hosts = models.ManyToManyField(ManagedServer, verbose_name=_('소속 서버'), blank=True)
    created_at = models.DateTimeField(_('생성일'), auto_now_add=True)

    class Meta:
        verbose_name = _('호스트 그룹')
        verbose_name_plural = _('호스트 그룹 목록')

    def __str__(self):
        return self.name

class AlertRule(models.Model):
    CONDITION_CHOICES = [
        ('gt', _('보다 큼 (> )')),
        ('lt', _('보다 작음 (< )')),
        ('eq', _('같음 (=)')),
        ('host_down', _('호스트 오프라인')),
    ]
    SEVERITY_CHOICES = [
        ('info', _('정보')),
        ('warning', _('경고')),
        ('critical', _('위험')),
    ]
    TARGET_TYPE_CHOICES = [
        ('single', _('개별 호스트')),
        ('group', _('호스트 그룹')),
        ('all', _('전체 호스트')),
    ]
    name = models.CharField(_('알람 이름'), max_length=100)
    target_type = models.CharField(_('대상 타입'), max_length=20, choices=TARGET_TYPE_CHOICES, default='single')
    hostname = models.CharField(_('호스트명'), max_length=100, db_index=True, blank=True, default='')
    host_group = models.ForeignKey(HostGroup, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_('대상 그룹'))
    
    metric_source = models.CharField(_('메트릭 소스'), max_length=50)
    metric_field = models.CharField(_('메트릭 필드'), max_length=100)
    filter_key = models.CharField(_('필터 키'), max_length=50, blank=True, default='')
    filter_value = models.CharField(_('필터 값'), max_length=100, blank=True, default='')
    log_keyword = models.CharField(_('로그 키워드'), max_length=100, blank=True, default='')
    
    condition = models.CharField(_('조건'), max_length=20, choices=CONDITION_CHOICES)
    threshold = models.FloatField(_('임계치'), default=0.0)
    severity = models.CharField(_('심각도'), max_length=20, choices=SEVERITY_CHOICES, default='warning')
    
    webhook_url = models.URLField(_('웹훅 URL'), blank=True, null=True)
    notification_email = models.CharField(_('알림 이메일'), max_length=500, blank=True, null=True, help_text=_("쉼표(,)로 구분하여 여러 이메일 지정 가능"))
    
    cooldown_minutes = models.IntegerField(_('쿨다운(분)'), default=5)
    last_triggered_at = models.DateTimeField(_('최근 발생 시각'), blank=True, null=True)
    is_active = models.BooleanField(_('활성화 여부'), default=True)
    created_at = models.DateTimeField(_('생성일'), auto_now_add=True)

    class Meta:
        verbose_name = _('알람 규칙')
        verbose_name_plural = _('알람 규칙 목록')

    def __str__(self):
        return f"{self.name} on {self.hostname}"

    def save(self, *args, **kwargs):
        """AlertRule 저장 시 캐시 무효화"""
        super().save(*args, **kwargs)
        # 캐시 무효화 (views.py의 invalidate_alert_rules_cache 함수 호출)
        from django.core.cache import cache
        # 모든 alert_rules 캐시 삭제
        for metric_source in ['STATUS_API', 'STATUS_PING', 'host_down', None]:
            for condition in ['status_check', 'host_down', None]:
                for is_active in [True, False, None]:
                    cache.delete(f"alert_rules:{metric_source}:{condition}:{is_active}")
        # 일반 활성 규칙 캐시도 삭제
        cache.delete('active_alert_rules')

    def delete(self, *args, **kwargs):
        """AlertRule 삭제 시 캐시 무효화"""
        super().delete(*args, **kwargs)
        # 캐시 무효화
        from django.core.cache import cache
        # 모든 alert_rules 캐시 삭제
        for metric_source in ['STATUS_API', 'STATUS_PING', 'host_down', None]:
            for condition in ['status_check', 'host_down', None]:
                for is_active in [True, False, None]:
                    cache.delete(f"alert_rules:{metric_source}:{condition}:{is_active}")
        # 일반 활성 규칙 캐시도 삭제
        cache.delete('active_alert_rules')

class AlertHistory(models.Model):
    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, verbose_name=_('규칙'))
    hostname = models.CharField(_('호스트명'), max_length=100, db_index=True, default='')
    value = models.FloatField(_('발생 당시 값'), null=True, blank=True)
    severity = models.CharField(_('심각도'), max_length=20, default='warning')
    timestamp = models.DateTimeField(_('발생 시각'), auto_now_add=True, db_index=True)
    is_resolved = models.BooleanField(_('해결 여부'), default=False)
    resolved_at = models.DateTimeField(_('해결 시각'), null=True, blank=True)

    class Meta:
        verbose_name = _('알람 이력')
        verbose_name_plural = _('알람 이력 목록')
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.rule.name} on {self.hostname}"

class MetricMetadata(models.Model):
    hostname = models.CharField(_('호스트명'), max_length=100, db_index=True)
    source = models.CharField(_('소스'), max_length=100, db_index=True)
    last_seen = models.DateTimeField(_('최근 감지 시각'), auto_now=True)
    metadata = models.JSONField(_('메타데이터'), default=dict)

    class Meta:
        verbose_name = _('메트릭 메타데이터')
        verbose_name_plural = _('메트릭 메타데이터 목록')
        unique_together = ('hostname', 'source')

class DataCollectionConfig(models.Model):
    CONFIG_TYPES = [
        ('linux', _('리눅스 공통')),
        ('windows', _('윈도우 공통')),
        ('custom', _('서버 전용')),
    ]
    server = models.ForeignKey(ManagedServer, on_delete=models.CASCADE, null=True, blank=True, verbose_name=_('서버'))
    config_type = models.CharField(_('설정 타입'), max_length=20, choices=CONFIG_TYPES, default='custom')
    config = models.JSONField(_('설정 JSON'), default=dict, help_text=_("데이터 수집(메트릭/로그 저장 규칙)을 위한 JSON 설정"))
    
    # migrations stability
    is_active = models.BooleanField(_('활성화 여부'), default=True)

    class Meta:
        verbose_name = _('데이터 수집 설정')
        verbose_name_plural = _('데이터 수집 설정 목록')
        unique_together = ('server', 'config_type')

    def __str__(self):
        if self.server:
            return f"Data Config for {self.server.hostname}"
        return f"Common Data Config ({self.get_config_type_display()})"

class MetricAggregate(models.Model):
    AGG_TYPE_CHOICES = [
        ('avg', _('평균')),
        ('max', _('최대')),
        ('min', _('최소')),
    ]
    hostname = models.CharField(_('호스트명'), max_length=100, db_index=True)
    source = models.CharField(_('소스'), max_length=100, db_index=True)
    period_start = models.DateTimeField(_('기간 시작'), db_index=True)
    period_end = models.DateTimeField(_('기간 종료'))
    agg_type = models.CharField(_('집계 유형'), max_length=10, choices=AGG_TYPE_CHOICES)
    value = models.FloatField(_('값'), null=True)
    data = models.JSONField(_('메타데이터'), default=dict)
    created_at = models.DateTimeField(_('생성일'), auto_now_add=True)

    class Meta:
        verbose_name = _('메트릭 집계')
        verbose_name_plural = _('메트릭 집계 목록')
        indexes = [
            models.Index(fields=['hostname', 'source', 'period_start']),
        ]
        unique_together = ('hostname', 'source', 'period_start', 'period_end', 'agg_type')
