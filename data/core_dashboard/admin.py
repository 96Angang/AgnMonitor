from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from datetime import timedelta
from .models import (
    MonitoringMetric, DashboardPanel, ManagedServer, 
    AlertRule, AlertHistory, MetricMetadata, DataCollectionConfig
)

class AlertHistoryInline(admin.TabularInline):
    model = AlertHistory
    extra = 0
    readonly_fields = ('timestamp', 'severity', 'value')
    can_delete = False

@admin.register(MonitoringMetric)
class MonitoringMetricAdmin(admin.ModelAdmin):
    list_display = ('hostname', 'source', 'level', 'timestamp')
    list_filter = ('hostname', 'source', 'level', 'timestamp')
    search_fields = ('hostname', 'source')
    readonly_fields = ('timestamp',)
    date_hierarchy = 'timestamp'
    # Django default 'delete_selected' is already available, 
    # but we add targeted cleanup actions.
    actions = ['delete_all_metrics', 'delete_selected_server_metrics', 'delete_old_metrics']

    def delete_all_metrics(self, request, queryset):
        """전체 메트릭 데이터를 즉시 일괄 삭제합니다."""
        count = MonitoringMetric.objects.count()
        MonitoringMetric.objects.all().delete()
        self.message_user(request, f"전체 {count} 개의 메트릭 데이터가 완전히 삭제되었습니다.")
    delete_all_metrics.short_description = _("⚠️ 전체 데이터 삭제 (주의)")

    def delete_selected_server_metrics(self, request, queryset):
        """선택된 항목들의 호스트네임을 기준으로 해당 서버의 모든 데이터를 삭제합니다."""
        hostnames = list(queryset.values_list('hostname', flat=True).distinct())
        if not hostnames:
            return
        
        total_deleted = 0
        for host in hostnames:
            deleted, _ = MonitoringMetric.objects.filter(hostname=host).delete()
            total_deleted += deleted
        
        self.message_user(request, f"선택된 서버({', '.join(hostnames)})의 모든 데이터 {total_deleted}개가 삭제되었습니다.")
    delete_selected_server_metrics.short_description = _("선택된 항목의 해당 서버 데이터 전체 삭제")

    def delete_old_metrics(self, request, queryset):
        # 30일 이전 데이터 삭제 (기본값)
        threshold = timezone.now() - timedelta(days=30)
        deleted, _ = MonitoringMetric.objects.filter(timestamp__lt=threshold).delete()
        self.message_user(request, f"{deleted} 개의 오래된 메트릭이 삭제되었습니다.")
    delete_old_metrics.short_description = _("30일 이상 된 메트릭 삭제")

@admin.register(DashboardPanel)
class DashboardPanelAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'host', 'metric_source', 'metric_field', 'chart_type', 'x', 'y', 'w', 'h')
    list_filter = ('user', 'host', 'metric_source', 'chart_type')
    search_fields = ('title', 'host', 'metric_source', 'metric_field')

@admin.register(ManagedServer)
class ManagedServerAdmin(admin.ModelAdmin):
    list_display = ('nickname', 'hostname', 'last_detected_ip', 'reachable_ip', 'retention_days', 'created_at')
    list_filter = ('retention_days', 'created_at')
    search_fields = ('nickname', 'hostname', 'last_detected_ip', 'reachable_ip')
    ordering = ('hostname',)

@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'hostname', 'severity', 'condition', 'is_active', 'last_triggered_at')
    list_filter = ('hostname', 'severity', 'condition', 'is_active')
    search_fields = ('name', 'hostname', 'metric_source', 'metric_field')
    inlines = [AlertHistoryInline]
    actions = ['activate_rules', 'deactivate_rules']

    def activate_rules(self, request, queryset):
        queryset.update(is_active=True)
    activate_rules.short_description = _("선택된 알람 규칙 활성화")

    def deactivate_rules(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_rules.short_description = _("선택된 알람 규칙 비활성화")

@admin.register(AlertHistory)
class AlertHistoryAdmin(admin.ModelAdmin):
    list_display = ('rule', 'severity', 'timestamp', 'is_resolved', 'resolved_at')
    list_filter = ('severity', 'is_resolved', 'timestamp')
    search_fields = ('rule__name', 'rule__hostname')
    readonly_fields = ('timestamp',)
    actions = ['mark_as_resolved']

    def mark_as_resolved(self, request, queryset):
        queryset.update(is_resolved=True, resolved_at=timezone.now())
    mark_as_resolved.short_description = _("선택된 알람 해결 완료 처리")

@admin.register(MetricMetadata)
class MetricMetadataAdmin(admin.ModelAdmin):
    list_display = ('hostname', 'source', 'last_seen')
    list_filter = ('hostname', 'source', 'last_seen')
    search_fields = ('hostname', 'source')

@admin.register(DataCollectionConfig)
class DataCollectionConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'config_type', 'is_active')
    list_filter = ('config_type', 'is_active')
    search_fields = ('server__hostname', 'server__nickname')
