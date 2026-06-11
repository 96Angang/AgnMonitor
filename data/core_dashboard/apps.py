import os
from django.apps import AppConfig

class CoreDashboardConfig(AppConfig):
    name = 'core_dashboard'

    def ready(self):
        # Celery가 로드되면 autodiscover_tasks()를 통해 task들을 감지하므로 
        # 별도의 스레드 시작 로직은 불필요합니다.
        pass
