import json
import logging
import requests
import re
from datetime import timedelta
from django.utils import timezone
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async

from .models import MonitoringMetric, DashboardPanel, ManagedServer, MetricMetadata, AlertRule, AlertHistory, HostGroup

logger = logging.getLogger(__name__)

class MonitoringConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_panels = []
        self.host_filter = None
        self.subscribed_hosts = set()  # Track subscribed host groups

    async def connect(self):
        if self.scope["user"].is_authenticated:
            self.user_group = f"user_{self.scope['user'].id}"
            if self.channel_layer:
                await self.channel_layer.group_add(self.user_group, self.channel_name)
                await self.channel_layer.group_add("realtime_metrics", self.channel_name)
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'user_group') and self.channel_layer:
            await self.channel_layer.group_discard(self.user_group, self.channel_name)
            await self.channel_layer.group_discard("realtime_metrics", self.channel_name)
            # Discard all host-specific groups
            for host in self.subscribed_hosts:
                await self.channel_layer.group_discard(f"host_{host}", self.channel_name)

    async def update_host_subscriptions(self, hosts_to_subscribe):
        """Update WebSocket group subscriptions based on user panels (Phase 3 optimization)"""
        if not self.channel_layer:
            return

        hosts_to_subscribe = set(hosts_to_subscribe)
        hosts_to_add = hosts_to_subscribe - self.subscribed_hosts
        hosts_to_remove = self.subscribed_hosts - hosts_to_subscribe

        # Subscribe to new hosts
        for host in hosts_to_add:
            await self.channel_layer.group_add(f"host_{host}", self.channel_name)

        # Unsubscribe from removed hosts
        for host in hosts_to_remove:
            await self.channel_layer.group_discard(f"host_{host}", self.channel_name)

        self.subscribed_hosts = hosts_to_subscribe

    async def broadcast_metrics(self, event):
        """
        Receives raw metrics from views.py and filters them for the current user's panels.
        """
        if not self.user_panels:
            return

        raw_metrics = event['metrics']
        filtered_results = {}
        
        for item in raw_metrics:
            source = item.get('name')
            tags = item.get('tags', {})
            host = tags.get('host')
            fields = item.get('fields', {})
            
            for panel in self.user_panels:
                if panel['host'] == host and panel['metric_source'] == source:
                    # Tag filtering
                    if panel['filter_key'] and panel['filter_value']:
                        if str(tags.get(panel['filter_key'])) != str(panel['filter_value']):
                            continue
                    
                    p_id = panel['id']
                    field_name = panel['metric_field']
                    
                    if source == 'custom_logs':
                        log_val = fields.get('value', '')
                        if panel.get('log_filter') and panel['log_filter'].lower() not in log_val.lower():
                            continue
                        ts = timezone.localtime(timezone.now()).strftime('%H:%M:%S')
                        filtered_results[p_id] = [f"[{ts}] {log_val}"]
                    elif source in ['net', 'diskio', 'cpu', 'win_cpu'] or source.startswith('sql_'):
                        val = fields.get(field_name)
                        if val is not None:
                            if source == 'cpu' and field_name == 'usage_idle':
                                val = 100 - val
                            
                            if p_id not in filtered_results:
                                filtered_results[p_id] = []
                            filtered_results[p_id].append({'v': val, 't': tags})
                    else:
                        val = fields.get(field_name)
                        if val is not None:
                            filtered_results[p_id] = val

        if filtered_results:
            await self.send(text_data=json.dumps({
                'type': 'metrics',
                'metrics': filtered_results,
                'is_realtime': True
            }))

    async def alert_notification(self, event):
        await self.send(text_data=json.dumps({
            'type': 'alert',
            'alert': event['alert']
        }))

    async def server_status_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'server_status_update',
            'hostname': event['hostname'],
            'status_api': event['status_api'],
            'status_ping': event['status_ping']
        }))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            action = data.get('action')
            
            if action == 'get_initial_data':
                self.host_filter = data.get('host_filter')
                hosts = await self.get_available_hosts()
                host_groups = await self.get_host_groups()
                nickname_map = {h['hostname']: h['nickname'] for h in hosts if h['nickname']}
                self.user_panels = await self.get_user_panels(self.host_filter)
                alert_rules = await self.get_alert_rules()
                initial_metrics = await self.get_metrics_for_panels(self.user_panels)

                # Phase 3: Subscribe to host-specific groups based on panels
                hosts_in_panels = set(p['host'] for p in self.user_panels if p.get('host'))
                await self.update_host_subscriptions(hosts_in_panels)

                await self.send(text_data=json.dumps({
                    'type': 'setup',
                    'hosts': hosts,
                    'host_groups': host_groups,
                    'nickname_map': nickname_map,
                    'panels': self.user_panels,
                    'alert_rules': alert_rules,
                    'initial_metrics': initial_metrics,
                    'host_filter': self.host_filter
                }))
            elif action == 'save_layout':
                await self.update_panel_layouts(data.get('layout', []))
                # Phase 3: Refresh host subscriptions after layout changes
                self.user_panels = await self.get_user_panels(self.host_filter)
                hosts_in_panels = set(p['host'] for p in self.user_panels if p.get('host'))
                await self.update_host_subscriptions(hosts_in_panels)
            elif action == 'add_panel':
                new_panel = await self.create_panel(data.get('panel'))
                self.user_panels = await self.get_user_panels(self.host_filter)
                # Phase 3: Update host subscriptions after panel changes
                hosts_in_panels = set(p['host'] for p in self.user_panels if p.get('host'))
                await self.update_host_subscriptions(hosts_in_panels)
                await self.send(text_data=json.dumps({'type': 'panel_added', 'panel': new_panel}))
            elif action == 'update_memo':
                await self.edit_memo(data.get('id'), data.get('content'))
                self.user_panels = await self.get_user_panels(self.host_filter)
                await self.send(text_data=json.dumps({'type': 'memo_updated', 'id': data.get('id'), 'content': data.get('content')}))
            elif action == 'delete_panel':
                await self.remove_panel(data.get('id'))
                self.user_panels = await self.get_user_panels(self.host_filter)
                # Phase 3: Update host subscriptions after panel changes
                hosts_in_panels = set(p['host'] for p in self.user_panels if p.get('host'))
                await self.update_host_subscriptions(hosts_in_panels)
                await self.send(text_data=json.dumps({'type': 'panel_deleted', 'id': data.get('id')}))
            elif action == 'get_metrics':
                panels = await self.get_user_panels(self.host_filter)
                metrics = await self.get_metrics_for_panels(panels)
                summary = await self.get_alert_summary()
                await self.send(text_data=json.dumps({
                    'type': 'metrics', 
                    'metrics': metrics, 
                    'alert_summary': summary
                }))
            elif action == 'get_available_filters':
                filters = await self.discover_filters(data.get('host'), data.get('source'))
                await self.send(text_data=json.dumps({'type': 'available_filters', 'filters': filters}))
            elif action == 'add_alert_rule':
                rule = await self.create_alert_rule(data.get('rule'))
                await self.send(text_data=json.dumps({'type': 'alert_rule_added', 'rule': rule}))
            elif action == 'delete_alert_rule':
                await self.remove_alert_rule(data.get('id'))
                await self.send(text_data=json.dumps({'type': 'alert_rule_deleted', 'id': data.get('id')}))
            elif action == 'batch_delete_alert_rules':
                await self.remove_batch_alert_rules(data.get('ids', []))
                await self.send(text_data=json.dumps({'type': 'alert_rule_deleted', 'ids': data.get('ids')}))
            elif action == 'edit_alert_rule':
                rule = await self.update_alert_rule(data.get('id'), data.get('rule'))
                if rule:
                    await self.send(text_data=json.dumps({'type': 'alert_rule_updated', 'rule': rule}))
            elif action == 'toggle_alert_rule':
                await self.toggle_rule_status(data.get('id'))
                await self.send(text_data=json.dumps({'type': 'alert_rule_updated'}))
            elif action == 'get_more_logs':
                await self.handle_get_more_logs(data)
            elif action == 'get_alert_history':
                history = await self.get_alert_history()
                await self.send(text_data=json.dumps({'type': 'alert_history', 'history': history}))
            elif action == 'resolve_alert':
                await self.mark_alert_resolved(data.get('id'))
                await self.send(text_data=json.dumps({'type': 'alert_resolved', 'id': data.get('id')}))
            elif action == 'resolve_all_alerts':
                await self.mark_all_resolved()
                await self.send(text_data=json.dumps({'type': 'all_alerts_resolved'}))
            elif action == 'get_resolved_history':
                history = await self.get_resolved_history()
                await self.send(text_data=json.dumps({'type': 'resolved_history', 'history': history}))
            elif action == 'clear_alert_history':
                await self.remove_alert_history()
                await self.send(text_data=json.dumps({'type': 'alert_history_cleared'}))
            elif action == 'get_managed_servers':
                result = await self.get_managed_servers()
                await self.send(text_data=json.dumps({
                    'type': 'managed_servers', 
                    'servers': result['servers'],
                    'db_size': result['db_size']
                }))
            elif action == 'register_server':
                server = await self.register_managed_server(data.get('hostname'), data.get('nickname'))
                await self.send(text_data=json.dumps({'type': 'server_registered', 'server': server}))
            elif action == 'update_server':
                server = await self.update_managed_server(data.get('hostname'), data.get('nickname'))
                await self.send(text_data=json.dumps({'type': 'server_updated', 'server': server}))
            elif action == 'delete_server':
                await self.delete_managed_server(data.get('hostname'))
                await self.send(text_data=json.dumps({'type': 'server_deleted', 'hostname': data.get('hostname')}))
            elif action == 'batch_delete_servers':
                await self.batch_delete_managed_servers(data.get('hostnames', []))
                await self.send(text_data=json.dumps({'type': 'server_deleted', 'hostnames': data.get('hostnames')}))
            elif action == 'batch_register_servers':
                await self.batch_register_managed_servers(data.get('hostnames', []))
                await self.send(text_data=json.dumps({'type': 'server_registered'}))
            elif action == 'update_server_retention':
                await self.update_server_retention(data.get('hostname'), data.get('days'))
                await self.send(text_data=json.dumps({'type': 'retention_updated'}))
            elif action == 'batch_update_retention':
                await self.batch_update_retention(data.get('hostnames', []), data.get('days'))
                await self.send(text_data=json.dumps({'type': 'retention_updated'}))
            elif action == 'test_notification':
                await self.handle_test_notification(data)
            elif action == 'refresh_server_ip':
                result = await self.trigger_ip_refresh(data.get('hostname'))
                await self.send(text_data=json.dumps({'type': 'ip_refreshed', 'hostname': data.get('hostname'), 'ip': result}))
            elif action == 'clear_server_ip':
                await self.reset_server_ip(data.get('hostname'))
                await self.send(text_data=json.dumps({'type': 'ip_refreshed', 'hostname': data.get('hostname'), 'ip': "Unknown"}))
            elif action == 'get_historical_metrics':
                await self.handle_get_historical_metrics(data)
            elif action == 'add_host_group':
                group = await self.create_host_group(data.get('name'), data.get('hostnames', []))
                await self.send(text_data=json.dumps({'type': 'host_group_added', 'group': group}))
            elif action == 'edit_host_group':
                group = await self.update_host_group(data.get('id'), data.get('name'), data.get('hostnames', []))
                await self.send(text_data=json.dumps({'type': 'host_group_updated', 'group': group}))
            elif action == 'delete_host_group':
                await self.remove_host_group(data.get('id'))
                await self.send(text_data=json.dumps({'type': 'host_group_deleted', 'id': data.get('id')}))
        except Exception as e:
            logger.error(f"Error in receive: {e}", exc_info=True)

    async def handle_get_historical_metrics(self, data):
        hostname = data.get('host') # Can be a string or a list
        source = data.get('source')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        search_query = data.get('search_query')
        offset = data.get('offset', 0)
        limit = data.get('limit', 100)
        
        metrics = await self.get_historical_metrics_data(hostname, source, start_date, end_date, search_query, offset, limit)
        await self.send(text_data=json.dumps({
            'type': 'historical_metrics',
            'metrics': metrics,
            'is_append': offset > 0
        }))

    @database_sync_to_async
    def get_historical_metrics_data(self, hostname, source, start_date=None, end_date=None, search_query=None, offset=0, limit=100):
        if isinstance(hostname, list):
            qs = MonitoringMetric.objects.filter(hostname__in=hostname)
        else:
            qs = MonitoringMetric.objects.filter(hostname=hostname)
            
        if source and source != 'all':
            qs = qs.filter(source=source)
        
        if start_date:
            try:
                # Expects 'YYYY-MM-DDTHH:MM' format from datetime-local input
                qs = qs.filter(timestamp__gte=start_date.replace('T', ' '))
            except: pass
        if end_date:
            try:
                qs = qs.filter(timestamp__lte=end_date.replace('T', ' '))
            except: pass
            
        if search_query:
            # Filter by keys or values in data__fields JSON
            # This is a broad search across all field keys and values
            qs = qs.filter(data__fields__icontains=search_query)

        qs = qs.order_by('-timestamp')[offset:offset+limit]
        
        results = []
        for m in qs:
            results.append({
                'id': m.id,
                'source': m.source,
                'timestamp': timezone.localtime(m.timestamp).strftime('%Y-%m-%d %H:%M:%S'),
                'data': m.data
            })
        return results

    @database_sync_to_async
    def reset_server_ip(self, hostname):
        ManagedServer.objects.filter(hostname=hostname).update(last_detected_ip=None)
        metas = MetricMetadata.objects.filter(hostname=hostname)
        for m in metas:
            if 'reachable_ip' in m.metadata:
                del m.metadata['reachable_ip']
                m.save()

    async def trigger_ip_refresh(self, hostname):
        def get_ip_info():
            s = ManagedServer.objects.filter(hostname=hostname).first()
            if not s:
                return None, None

            # Use last_detected_ip if available, else fallback to reachable_ip
            public_ip = s.last_detected_ip
            reachable_ip = s.reachable_ip

            return public_ip, reachable_ip

        public_ip, reachable_ip = await database_sync_to_async(get_ip_info)()

        # In Docker, reachable_ip might be the internal container IP or the proxy's IP.
        # But for agent communication (Telegraf), we need the IP where Telegraf is listening.
        target_ip = public_ip or reachable_ip

        if not target_ip or target_ip == "Unknown":
            return "No IP Info"

        def fetch_real_public_ip():
            try:
                # Attempt to fetch from Telegraf's Prometheus output if configured
                url = f"http://{target_ip}:9126/metrics"
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    import re
                    match = re.search(r'public_ip[^{]*\{[^}]*value="([^"]+)"[^}]*\}', response.text)
                    if match: return match.group(1)

                # If metrics endpoint doesn't have it, maybe it's just a regular host we can't 'refresh' via API
                return "No public_ip metric found"
            except requests.exceptions.ConnectionError:
                return "Connection Refused"
            except Exception as e:
                return f"Error: {str(e)}"

        real_ip = await sync_to_async(fetch_real_public_ip, thread_sensitive=False)()

        # If we got a valid-looking IP, update it
        if real_ip and "." in real_ip and not any(x in real_ip for x in ["Error", "Refused", "found"]):
            await database_sync_to_async(lambda: ManagedServer.objects.filter(hostname=hostname).update(last_detected_ip=real_ip))()
            return real_ip

        return real_ip
    async def handle_test_notification(self, data):
        from .notifications import send_email_notification, send_webhook_notification
        import threading
        test_data = {
            "rule_name": "Test Alert", "hostname": "TEST-HOST", "metric": "test.metric",
            "value": 99.9, "threshold": 90.0, "severity": "critical",
            "timestamp": timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')
        }
        if data['type'] == 'webhook':
            threading.Thread(target=send_webhook_notification, args=(data['target'], test_data), daemon=True).start()
        elif data['type'] == 'email':
            threading.Thread(target=send_email_notification, args=(data['target'], test_data), daemon=True).start()

    async def handle_get_more_logs(self, data):
        panel_id = data.get('panel_id')
        offset = data.get('offset', 0)
        panel = await self.get_panel_by_id(panel_id)
        if not panel: return
        logs = await self.get_historical_logs_data(panel, offset)
        await self.send(text_data=json.dumps({'type': 'more_logs', 'panel_id': panel_id, 'logs': logs}))

    @database_sync_to_async
    def get_panel_by_id(self, panel_id):
        return DashboardPanel.objects.filter(id=panel_id).values('host', 'metric_source', 'log_filter').first()

    @database_sync_to_async
    def get_historical_logs_data(self, panel, offset):
        qs = MonitoringMetric.objects.filter(hostname=panel['host'], source=panel['metric_source']).order_by('-timestamp')
        if panel.get('log_filter'):
            qs = qs.filter(data__fields__value__icontains=panel['log_filter'].lower())
        return [m.data.get('fields', {}).get('value', '') for m in qs[offset:offset+10]]

    @database_sync_to_async
    def toggle_rule_status(self, rule_id):
        rule = AlertRule.objects.filter(id=rule_id).first()
        if rule:
            rule.is_active = not rule.is_active
            rule.save(update_fields=['is_active'])

    @database_sync_to_async
    def mark_alert_resolved(self, alert_id):
        AlertHistory.objects.filter(id=alert_id).update(is_resolved=True, resolved_at=timezone.now())

    @database_sync_to_async
    def mark_all_resolved(self):
        AlertHistory.objects.filter(is_resolved=False).update(is_resolved=True, resolved_at=timezone.now())

    @database_sync_to_async
    def remove_alert_history(self):
        AlertHistory.objects.all().delete()

    @database_sync_to_async
    def get_host_groups(self):
        groups = HostGroup.objects.all().prefetch_related('hosts')
        return [{
            'id': g.id,
            'name': g.name,
            'hostnames': [h.hostname for h in g.hosts.all()]
        } for g in groups]

    @database_sync_to_async
    def create_host_group(self, name, hostnames):
        g = HostGroup.objects.create(name=name)
        if hostnames:
            servers = ManagedServer.objects.filter(hostname__in=hostnames)
            g.hosts.set(servers)
        return {'id': g.id, 'name': g.name, 'hostnames': hostnames}

    @database_sync_to_async
    def update_host_group(self, group_id, name, hostnames):
        g = HostGroup.objects.filter(id=group_id).first()
        if g:
            g.name = name
            g.save()
            servers = ManagedServer.objects.filter(hostname__in=hostnames)
            g.hosts.set(servers)
            return {'id': g.id, 'name': g.name, 'hostnames': hostnames}
        return None

    @database_sync_to_async
    def remove_host_group(self, group_id):
        HostGroup.objects.filter(id=group_id).delete()

    @database_sync_to_async
    def get_alert_rules(self):
        return list(AlertRule.objects.all().values(
            'id', 'name', 'target_type', 'hostname', 'host_group_id', 
            'metric_source', 'metric_field', 'filter_key', 
            'filter_value', 'condition', 'threshold', 'log_keyword', 'severity', 
            'webhook_url', 'notification_email', 'cooldown_minutes', 'is_active'
        ))

    @database_sync_to_async
    def create_alert_rule(self, data):
        r = AlertRule.objects.create(
            name=data['name'], 
            target_type=data.get('target_type', 'single'),
            hostname=data.get('host') if data.get('target_type', 'single') == 'single' else '',
            host_group_id=data.get('host_group_id') if data.get('target_type') == 'group' else None,
            metric_source=data['source'],
            metric_field=data['field'], filter_key=data.get('f_key') or '',
            filter_value=data.get('f_val') or '', condition=data.get('condition', 'gt'),
            threshold=float(data['threshold']) if data.get('threshold') else 0.0,
            log_keyword=data.get('log_keyword'), severity=data.get('severity', 'warning'),
            webhook_url=data.get('webhook_url'), notification_email=data.get('notification_email'),
            cooldown_minutes=int(data.get('cooldown', 5))
        )
        return {'id': r.id, 'name': r.name, 'is_active': r.is_active}

    @database_sync_to_async
    def remove_alert_rule(self, rule_id):
        AlertRule.objects.filter(id=rule_id).delete()

    @database_sync_to_async
    def remove_batch_alert_rules(self, rule_ids):
        AlertRule.objects.filter(id__in=rule_ids).delete()

    @database_sync_to_async
    def update_alert_rule(self, rule_id, data):
        rule = AlertRule.objects.filter(id=rule_id).first()
        if not rule: return None
        rule.name = data['name']
        rule.target_type = data.get('target_type', 'single')
        rule.hostname = data.get('host') if rule.target_type == 'single' else ''
        rule.host_group_id = data.get('host_group_id') if rule.target_type == 'group' else None
        
        rule.metric_source, rule.metric_field = data['source'], data['field']
        rule.filter_key, rule.filter_value = data.get('f_key') or '', data.get('f_val') or ''
        rule.condition = data.get('condition', 'gt')
        rule.threshold = float(data['threshold']) if data.get('threshold') else 0.0
        rule.log_keyword, rule.severity = data.get('log_keyword'), data.get('severity', 'warning')
        rule.webhook_url, rule.notification_email = data.get('webhook_url'), data.get('notification_email')
        rule.cooldown_minutes = int(data.get('cooldown', 5))
        rule.save()
        return {'id': rule.id, 'name': rule.name}

    @database_sync_to_async
    def get_alert_history(self):
        history = AlertHistory.objects.filter(is_resolved=False).select_related('rule', 'rule__host_group')[:50]
        results = []
        for h in history:
            target_label = "-"
            if h.rule:
                if h.rule.target_type == 'single':
                    target_label = h.rule.hostname
                elif h.rule.target_type == 'group' and h.rule.host_group:
                    target_label = f"Group: {h.rule.host_group.name}"
                elif h.rule.target_type == 'all':
                    target_label = "ALL"

            results.append({
                'id': h.id,
                'rule_name': h.rule.name if h.rule else "Deleted Rule",
                'rule_target': target_label,
                'origin': h.hostname or "-",
                'value': h.value,
                'severity': h.severity,
                'timestamp': timezone.localtime(h.timestamp).strftime('%Y-%m-%d %H:%M:%S')
            })
        return results

    @database_sync_to_async
    def get_resolved_history(self):
        history = AlertHistory.objects.filter(is_resolved=True).select_related('rule', 'rule__host_group').order_by('-resolved_at')[:50]
        results = []
        for h in history:
            target_label = "-"
            if h.rule:
                if h.rule.target_type == 'single':
                    target_label = h.rule.hostname
                elif h.rule.target_type == 'group' and h.rule.host_group:
                    target_label = f"Group: {h.rule.host_group.name}"
                elif h.rule.target_type == 'all':
                    target_label = "ALL"

            results.append({
                'id': h.id,
                'rule_name': h.rule.name if h.rule else "Deleted Rule",
                'rule_target': target_label,
                'origin': h.hostname or "-",
                'value': h.value,
                'severity': h.severity,
                'timestamp': timezone.localtime(h.timestamp).strftime('%m-%d %H:%M'),
                'resolved_at': timezone.localtime(h.resolved_at).strftime('%m-%d %H:%M') if h.resolved_at else '-'
            })
        return results

    @database_sync_to_async
    def discover_filters(self, host, source):
        try:
            if not host:
                all_sources = list(MetricMetadata.objects.values_list('source', flat=True).distinct())
                fields, tags_dict = [], {}
                if source:
                    metas = MetricMetadata.objects.filter(source=source)
                    fields_set = set()
                    for m in metas:
                        fields_set.update(m.metadata.get('fields', []))
                        for k, v_list in m.metadata.get('tags', {}).items():
                            if k not in tags_dict: tags_dict[k] = set()
                            tags_dict[k].update(v_list if isinstance(v_list, list) else [v_list])
                    fields = sorted(list(fields_set))
                    for k in tags_dict: tags_dict[k] = sorted(list(tags_dict[k]))
                return {'tags': tags_dict, 'fields': fields, 'host_sources': sorted(all_sources)}

            if isinstance(host, list):
                metas = MetricMetadata.objects.filter(hostname__in=host)
                host_sources = sorted(list(metas.values_list('source', flat=True).distinct()))
                fields_set = set()
                tags_dict = {}
                if source:
                    metas = metas.filter(source=source)
                    for m in metas:
                        fields_set.update(m.metadata.get('fields', []))
                        for k, v_list in m.metadata.get('tags', {}).items():
                            if k not in tags_dict: tags_dict[k] = set()
                            tags_dict[k].update(v_list if isinstance(v_list, list) else [v_list])
                return {
                    'tags': {k: sorted(list(v)) for k, v in tags_dict.items()},
                    'fields': sorted(list(fields_set)),
                    'host_sources': host_sources
                }

            meta = MetricMetadata.objects.filter(hostname=host, source=source).first()
            host_sources = sorted(list(MetricMetadata.objects.filter(hostname=host).values_list('source', flat=True).distinct()))
            return {
                'tags': meta.metadata.get('tags', {}) if meta else {},
                'fields': meta.metadata.get('fields', []) if meta else [],
                'host_sources': host_sources
            }
        except: return {'tags': {}, 'fields': [], 'host_sources': []}

    @database_sync_to_async
    def get_available_hosts(self):
        try:
            managed_map = {s.hostname: s.nickname for s in ManagedServer.objects.all()}
            detected_hostnames = list(MetricMetadata.objects.values_list('hostname', flat=True).distinct())
            
            all_hostnames = set(managed_map.keys()) | set(detected_hostnames)
            results = []
            for h in all_hostnames:
                if not h or h == 'unknown': continue
                results.append({
                    'hostname': h,
                    'nickname': managed_map.get(h) or ''
                })
            
            # If nothing found in DB, but there are panels, at least show those hosts
            if not results:
                panel_hosts = list(DashboardPanel.objects.values_list('host', flat=True).distinct())
                for ph in panel_hosts:
                    if ph: results.append({'hostname': ph, 'nickname': ''})
            
            return sorted(results, key=lambda x: (x['nickname'] or x['hostname']).lower())
        except Exception as e:
            logger.error(f"Error in get_available_hosts: {e}")
            return []

    @database_sync_to_async
    def get_alert_summary(self):
        yesterday = timezone.now() - timedelta(days=1)
        return AlertHistory.objects.filter(timestamp__gt=yesterday).count()

    @database_sync_to_async
    def get_user_panels(self, host_filter=None):
        qs = DashboardPanel.objects.filter(user=self.scope['user'])
        if host_filter:
            qs = qs.filter(parent_host=host_filter)
        else:
            qs = qs.filter(parent_host__in=['', None])
        return list(qs.values('id', 'title', 'host', 'metric_source', 'metric_field', 'chart_type', 'filter_key', 'filter_value', 'log_filter', 'memo_content', 'x', 'y', 'w', 'h'))

    @database_sync_to_async
    def edit_memo(self, panel_id, content):
        DashboardPanel.objects.filter(id=panel_id).update(memo_content=content)

    @database_sync_to_async
    def update_panel_layouts(self, layout):
        for item in layout:
            DashboardPanel.objects.filter(id=item['id']).update(x=item['x'], y=item['y'], w=item['w'], h=item['h'])

    @database_sync_to_async
    def create_panel(self, data):
        chart_type = data.get('chart_type', 'text')
        source = data.get('source')
        user = self.scope['user']
        parent_host = data.get('parent_host', '')
        
        # Default sizes
        if source == 'custom_logs': p_w, p_h = 4, 3
        elif source == 'memo': p_w, p_h = 3, 2
        elif chart_type == 'line': p_w, p_h = 3, 3
        else: p_w, p_h = 2, 2
        
        # 새로고침 시에도 기존 레이아웃을 밀어내지 않도록 하단 좌표 계산
        last_panel = DashboardPanel.objects.filter(user=user, parent_host=parent_host).order_by('-y').first()
        next_y = (last_panel.y + last_panel.h) if last_panel else 0
            
        p = DashboardPanel.objects.create(
            user=user, title=data['title'], host=data['host'],
            parent_host=parent_host, metric_source=source,
            metric_field=data.get('field', ''), chart_type=chart_type,
            filter_key=data.get('f_key') or '', filter_value=data.get('f_val') or '',
            log_filter=data.get('log_filter') or '', memo_content=data.get('memo_content', ''),
            w=p_w, h=p_h, x=0, y=next_y
        )
        return {
            'id': p.id, 'title': p.title, 'host': p.host, 
            'metric_source': p.metric_source, 'metric_field': p.metric_field,
            'chart_type': p.chart_type, 'filter_key': p.filter_key, 
            'filter_value': p.filter_value, 'log_filter': p.log_filter,
            'memo_content': p.memo_content,
            'x': p.x, 'y': p.y, 'w': p.w, 'h': p.h
        }

    @database_sync_to_async
    def remove_panel(self, panel_id):
        DashboardPanel.objects.filter(id=panel_id).delete()

    async def get_managed_servers(self):
        data = await self.get_managed_servers_data()
        results = []
        for hostname in data['all_detected_hosts']:
            m_info = data['managed_hosts_dict'].get(hostname)
            is_registered = m_info is not None
            ip = m_info['last_detected_ip'] if is_registered and m_info['last_detected_ip'] else "Unknown"
            reachable_ip = m_info['reachable_ip'] if is_registered and m_info['reachable_ip'] else "Unknown"
            results.append({
                'hostname': hostname, 'nickname': m_info['nickname'] if is_registered else "",
                'ip': ip, 'reachable_ip': reachable_ip, 'is_registered': is_registered,
                'status_api': m_info['status_api'] if is_registered else "normal",
                'status_ping': m_info['status_ping'] if is_registered else "normal",
                'retention_days': m_info['retention_days'] if is_registered else 14,
                'created_at': m_info['created_at'] if is_registered else "Not Registered"
            })
        for h, info in data['managed_hosts_dict'].items():
            if h not in data['all_detected_hosts']:
                results.append({
                    'hostname': h, 'nickname': info['nickname'], 'ip': info['last_detected_ip'] or "Offline",
                    'reachable_ip': info['reachable_ip'] or "Offline",
                    'is_registered': True, 
                    'status_api': info['status_api'],
                    'status_ping': info['status_ping'],
                    'retention_days': info['retention_days'], 'created_at': info['created_at']
                })
        
        sorted_results = sorted(results, key=lambda x: (not x['is_registered'], (x['nickname'] or x['hostname']).lower()))
        return {'servers': sorted_results, 'db_size': data.get('db_size', 'Unknown')}

    @database_sync_to_async
    def get_managed_servers_data(self):
        from django.db import connection
        db_size = "0 B"
        try:
            with connection.cursor() as cursor:
                engine = connection.vendor
                if engine == 'postgresql':
                    cursor.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
                    db_size = cursor.fetchone()[0]
                elif engine == 'mysql':
                    cursor.execute("SELECT SUM(data_length + index_length) FROM information_schema.TABLES WHERE table_schema = DATABASE()")
                    size_bytes = cursor.fetchone()[0]
                    if size_bytes:
                        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                            if size_bytes < 1024:
                                db_size = f"{size_bytes:.1f} {unit}"
                                break
                            size_bytes /= 1024
                elif engine == 'sqlite':
                    import os
                    from django.conf import settings
                    db_path = settings.DATABASES['default']['NAME']
                    if os.path.exists(db_path):
                        size_bytes = os.path.getsize(db_path)
                        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                            if size_bytes < 1024:
                                db_size = f"{size_bytes:.1f} {unit}"
                                break
                            size_bytes /= 1024
        except Exception as e:
            logger.error(f"Error calculating DB size: {e}")

        managed_qs = ManagedServer.objects.all()
        managed_hosts_dict = {
            s.hostname: {
                'nickname': s.nickname, 'last_detected_ip': s.last_detected_ip,
                'reachable_ip': s.reachable_ip,
                'status_api': s.status_api,
                'status_ping': s.status_ping,
                'retention_days': s.retention_days,
                'created_at': timezone.localtime(s.created_at).strftime('%Y-%m-%d %H:%M')
            } for s in managed_qs
        }
        all_detected_hosts = list(MetricMetadata.objects.values_list('hostname', flat=True).distinct())
        return {
            'managed_hosts_dict': managed_hosts_dict, 
            'all_detected_hosts': all_detected_hosts,
            'db_size': db_size
        }

    @database_sync_to_async
    def register_managed_server(self, hostname, nickname):
        MonitoringMetric.objects.filter(hostname=hostname).delete()
        s, created = ManagedServer.objects.get_or_create(hostname=hostname, defaults={'nickname': nickname or ''})
        if not created and nickname:
            s.nickname = nickname
            s.save()
        return {'hostname': s.hostname, 'nickname': s.nickname}

    @database_sync_to_async
    def update_managed_server(self, hostname, nickname):
        s = ManagedServer.objects.filter(hostname=hostname).first()
        if s:
            s.nickname = nickname
            s.save()
            return {'hostname': s.hostname, 'nickname': s.nickname}
        return None

    @database_sync_to_async
    def delete_managed_server(self, hostname):
        ManagedServer.objects.filter(hostname=hostname).delete()
        MonitoringMetric.objects.filter(hostname=hostname).delete()
        DashboardPanel.objects.filter(host=hostname).delete()
        AlertRule.objects.filter(hostname=hostname).delete()
        MetricMetadata.objects.filter(hostname=hostname).delete()
        return True

    @database_sync_to_async
    def batch_delete_managed_servers(self, hostnames):
        ManagedServer.objects.filter(hostname__in=hostnames).delete()
        MonitoringMetric.objects.filter(hostname__in=hostnames).delete()
        DashboardPanel.objects.filter(host__in=hostnames).delete()
        AlertRule.objects.filter(hostname__in=hostnames).delete()
        MetricMetadata.objects.filter(hostname__in=hostnames).delete()
        return True

    @database_sync_to_async
    def batch_register_managed_servers(self, hostnames):
        for h in hostnames:
            MonitoringMetric.objects.filter(hostname=h).delete()
            ManagedServer.objects.get_or_create(hostname=h)
        return True

    @database_sync_to_async
    def update_server_retention(self, hostname, days):
        ManagedServer.objects.filter(hostname=hostname).update(retention_days=days)

    @database_sync_to_async
    def batch_update_retention(self, hostnames, days):
        ManagedServer.objects.filter(hostname__in=hostnames).update(retention_days=days)

    @database_sync_to_async
    def get_metrics_for_panels(self, panels):
        results = {}
        if not panels: return results
        now = timezone.now()
        
        for panel in panels:
            try:
                p_id, host, source, field = panel['id'], panel['host'], panel['metric_source'], panel['metric_field']
                c_type = panel.get('chart_type', 'text')
                
                if source == 'custom_logs':
                    # Fetch last 20 logs from DB for initial display
                    qs = MonitoringMetric.objects.filter(hostname=host, source='custom_logs').order_by('-timestamp')
                    if panel.get('log_filter'):
                        qs = qs.filter(data__fields__value__icontains=panel['log_filter'].lower())
                    
                    log_list = []
                    for m in qs[:20]:
                        val = m.data.get('fields', {}).get('value', '')
                        ts = timezone.localtime(m.timestamp).strftime('%H:%M:%S')
                        log_list.append(f"[{ts}] {val}")
                    
                    results[p_id] = list(reversed(log_list))
                    continue

                lookback = 600 if c_type == 'line' else 40
                limit = 41 if c_type == 'line' else 2
                
                filter_kwargs = {
                    'hostname': host, 'source': source,
                    'timestamp__gt': now - timedelta(seconds=lookback),
                    'data__fields__has_key': field
                }
                if panel.get('filter_key') and panel.get('filter_value'):
                    filter_kwargs[f'data__tags__{panel["filter_key"]}'] = panel['filter_value']
                
                panel_metrics = list(MonitoringMetric.objects.filter(**filter_kwargs).order_by('-timestamp')[:limit])

                if c_type == 'line':
                    chart_data = []
                    if source in ['net', 'diskio']:
                        for i in range(len(panel_metrics) - 1):
                            m_curr, m_prev = panel_metrics[i], panel_metrics[i+1]
                            t_diff = (m_curr.timestamp - m_prev.timestamp).total_seconds()
                            if 0.5 < t_diff < 60:
                                v_diff = m_curr.data.get("fields", {}).get(field, 0) - m_prev.data.get("fields", {}).get(field, 0)
                                if v_diff >= 0:
                                    chart_data.append({'v': v_diff / t_diff, 't': timezone.localtime(m_curr.timestamp).strftime('%H:%M:%S')})
                        chart_data.reverse()
                    else:
                        for m in reversed(panel_metrics):
                            val = m.data.get("fields", {}).get(field)
                            if source == 'cpu' and field == 'usage_idle': val = 100 - val
                            chart_data.append({'v': val, 't': timezone.localtime(m.timestamp).strftime('%H:%M:%S')})
                    
                    # [Fallback] 히스토리가 없으면 최신 값이라도 점 하나 찍어줌
                    if not chart_data:
                        meta = MetricMetadata.objects.filter(hostname=host, source=source).first()
                        if meta and 'latest_values' in meta.metadata:
                            latest_val = meta.metadata['latest_values'].get(field)
                            if latest_val is not None:
                                if source == 'cpu' and field == 'usage_idle': latest_val = 100 - latest_val
                                chart_data.append({'v': latest_val, 't': timezone.localtime(meta.last_seen).strftime('%H:%M:%S')})
                    
                    results[p_id] = chart_data
                else:
                    if source in ['net', 'diskio'] and len(panel_metrics) >= 2:
                        m_curr, m_prev = panel_metrics[0], panel_metrics[1]
                        t_diff = (m_curr.timestamp - m_prev.timestamp).total_seconds()
                        if 0.5 < t_diff < 60:
                            v_diff = m_curr.data.get("fields", {}).get(field, 0) - m_prev.data.get("fields", {}).get(field, 0)
                            results[p_id] = v_diff / t_diff if v_diff >= 0 else 0
                        else: results[p_id] = 0
                    elif panel_metrics:
                        val = panel_metrics[0].data.get("fields", {}).get(field)
                        if source == 'cpu' and field == 'usage_idle': val = 100 - val
                        results[p_id] = val
                    else:
                        # [Fallback] DB 히스토리가 없는 경우 최신값 가져옴
                        meta = MetricMetadata.objects.filter(hostname=host, source=source).first()
                        if meta and 'latest_values' in meta.metadata:
                            latest_val = meta.metadata['latest_values'].get(field)
                            if latest_val is not None:
                                if source == 'cpu' and field == 'usage_idle': latest_val = 100 - latest_val
                                results[p_id] = latest_val
                            else: results[p_id] = "N/A"
                        else: results[p_id] = "N/A"
            except: results[p_id] = "ERR"
        return results
