from django.contrib import admin
from django.urls import path, include
from core_dashboard import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    path('', views.index, name='index'),
    path('server/<str:hostname>/', views.server_dashboard, name='server_dashboard'),
    path('manage/', views.server_management, name='server_management'),
    path('data-collection/', views.data_collection_page, name='data_collection'),
    path('data-viewer/', views.data_viewer_page, name='data_viewer'),
    path('status-overview/', views.status_overview_page, name='status_overview'),
    path('status-overview/download/', views.status_overview_download, name='status_overview_download'),
    path('alerts/', views.alerts_page, name='alerts_page'),
    path('api/collect/', views.collect_metrics, name='collect_metrics'),
    path('api/data-collection/update/', views.update_data_config, name='update_data_config'),
    path('api/data-collection/get/', views.get_data_config, name='get_common_data_config'),
    path('api/data-collection/get/<int:server_id>/', views.get_data_config, name='get_server_data_config'),
    path('', include('django.contrib.auth.urls')),
]
