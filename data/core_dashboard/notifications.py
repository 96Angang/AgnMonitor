import json
import logging
import urllib.request
import threading
from django.core.mail import send_mail
from django.conf import settings

logger = logging.getLogger(__name__)

def send_email_notification(recipient, data):
    """
    이메일 알림 전송
    """
    try:
        subject = f"[AgnMonitor] Alert Triggered: {data['rule_name']}"
        message = (
            f"Alert Rule: {data['rule_name']}\n"
            f"Severity: {data['severity'].upper()}\n"
            f"Host: {data['hostname']}\n"
            f"Metric: {data['metric']}\n"
            f"Current Value: {data['value']}\n"
            f"Threshold/Keyword: {data.get('keyword') or data.get('threshold')}\n"
            f"Time: {data['timestamp']}\n\n"
            "이 알림은 AgnMonitor 모니터링 시스템에서 자동 발송되었습니다."
        )
        
        # Split by comma and strip whitespace
        recipient_list = [r.strip() for r in recipient.split(',') if r.strip()]
        
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipient_list,
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.error(f"Email delivery failed: {e}")
        return False

def send_webhook_notification(url, data):
    """
    외부 Webhook(Discord/Slack 등)으로 알림 전송
    """
    try:
        payload = {
            "content": f"⚠️ **Alert Triggered: {data['rule_name']}**",
            "embeds": [{
                "title": data['rule_name'],
                "color": 15158332 if data['severity'] == 'critical' else 15844367,
                "fields": [
                    {"name": "Host", "value": data['hostname'], "inline": True},
                    {"name": "Metric", "value": data['metric'], "inline": True},
                    {"name": "Value", "value": str(data['value']), "inline": True},
                    {"name": "Threshold/Keyword", "value": str(data.get('keyword') or data.get('threshold')), "inline": True}
                ],
                "footer": {"text": f"AgnMonitor • {data['timestamp']}"}
            }]
        }
        
        req = urllib.request.Request(url)
        req.add_header('Content-Type', 'application/json; charset=utf-8')
        jsondata = json.dumps(payload)
        jsondataasbytes = jsondata.encode('utf-8')
        req.add_header('Content-Length', len(jsondataasbytes))
        
        with urllib.request.urlopen(req, timeout=5) as response:
            pass
        return True
    except Exception as e:
        logger.error(f"Webhook delivery failed: {e}")
        return False

def send_notification_async(rule, alert_data):
    """
    Celery 워커가 없는 환경을 위해 Threading을 사용하여 비동기 전송 시뮬레이션
    """
    if rule.webhook_url:
        threading.Thread(
            target=send_webhook_notification, 
            args=(rule.webhook_url, alert_data), 
            daemon=True
        ).start()
    
    if rule.notification_email:
        threading.Thread(
            target=send_email_notification, 
            args=(rule.notification_email, alert_data), 
            daemon=True
        ).start()
