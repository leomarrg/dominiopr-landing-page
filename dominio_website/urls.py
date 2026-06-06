"""
URL configuration for dominio_website project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from landing import views

# Throttle login attempts (brute-force protection): 10 / 5 min / IP.
login_view = views._rate_limited('login', limit=10, window=300)(
    auth_views.LoginView.as_view(template_name='landing/dashboard_login.html')
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/demo/', views.demo_api, name='demo_api'),
    path('widget.js', views.widget_js, name='widget_js'),
    path('get-started/', views.get_started, name='get_started'),
    path('terms/', views.terms, name='terms'),
    path('privacy/', views.privacy, name='privacy'),

    # Branded internal dashboard (custom backend, not Django admin)
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/lead/<int:pk>/status/', views.lead_status, name='lead_status'),
    path('dashboard/lead/<int:pk>/email/', views.lead_email, name='lead_email'),
    path('dashboard/clients/', views.clients_list, name='clients_list'),
    path('dashboard/clients/new/', views.client_form, name='client_create'),
    path('dashboard/clients/<int:pk>/', views.client_form, name='client_edit'),
    path('dashboard/clients/<int:pk>/toggle/', views.client_toggle_active, name='client_toggle_active'),
    path('dashboard/password/', views.password_change, name='password_change'),
    path('dashboard/login/', login_view, name='login'),
    path('dashboard/logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
]

# Dev-only browser auto-reload — never registered in production.
if settings.DEBUG:
    urlpatterns.insert(1, path('__reload__/', include('django_browser_reload.urls')))
