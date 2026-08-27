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
from django.urls import include, path, reverse_lazy
from django.contrib.sitemaps.views import sitemap
from django.views.generic import RedirectView

from landing import platform_views, seo, views

# Throttle login attempts (brute-force protection): 10 / 5 min / IP.
login_view = views._rate_limited('login', limit=10, window=300)(
    auth_views.LoginView.as_view(template_name='landing/dashboard_login.html')
)

# "Forgot password" for dashboard users (clients + staff). The request form is
# throttled hard: 5 / 15 min / IP — it sends email and reveals nothing either way.
password_reset_view = views._rate_limited('pwreset', limit=5, window=900)(
    auth_views.PasswordResetView.as_view(
        template_name='landing/dashboard_password_reset.html',
        email_template_name='landing/emails/password_reset.txt',
        html_email_template_name='landing/emails/password_reset.html',
        subject_template_name='landing/emails/password_reset_subject.txt',
        success_url=reverse_lazy('password_reset_done'),
    )
)

# The dashboard login is throttled but /admin/login/ was not, and a superuser
# there sees EVERY tenant's leads, transcripts and billing. Same protection,
# tighter: an admin password is not something anyone types wrong five times.
admin.site.login = views._rate_limited('adminlogin', limit=5, window=300)(admin.site.login)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/demo/', views.demo_api, name='demo_api'),
    path('api/survey/', views.survey_api, name='survey_api'),
    path('api/stripe/webhook/', views.stripe_webhook, name='stripe_webhook'),
    path('widget.js', views.widget_js, name='widget_js'),
    path('get-started/', views.get_started, name='get_started'),
    path('bienvenida/', views.bienvenida, name='bienvenida'),
    path('terms/', views.terms, name='terms'),
    # SEO plumbing (public): crawler rules, sitemap, PWA-style manifest for icons.
    path('robots.txt', seo.robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap, {'sitemaps': seo.SITEMAPS}, name='sitemap'),
    path('site.webmanifest', seo.webmanifest, name='webmanifest'),
    path('privacy/', views.privacy, name='privacy'),
    # El sitio esta en espanol pero estas dos rutas nacieron en ingles, asi que
    # todo el mundo (nosotros incluidos) teclea el slug espanol y se come un
    # 404. Ya paso una vez con los enlaces legales del portal de Stripe. 301
    # permanente: no son URLs nuevas que indexar, son el mismo documento.
    path('terminos/', RedirectView.as_view(pattern_name='terms', permanent=True)),
    path('privacidad/', RedirectView.as_view(pattern_name='privacy', permanent=True)),

    # Branded internal dashboard (custom backend, not Django admin)
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/lead/<int:pk>/status/', views.lead_status, name='lead_status'),
    path('dashboard/lead/<int:pk>/email/', views.lead_email, name='lead_email'),
    path('dashboard/export.csv', platform_views.export_leads_csv, name='export_leads'),

    # Centralized client platform (Phase 2)
    path('dashboard/conversations/', platform_views.conversations, name='conversations'),
    path('dashboard/conversations/<int:pk>/', platform_views.conversation_detail,
         name='conversation_detail'),
    path('dashboard/conversations/<int:pk>/state/', platform_views.conversation_state,
         name='conversation_state'),
    path('dashboard/bookings/', platform_views.bookings_list, name='bookings'),
    path('dashboard/bookings/<int:pk>/status/', platform_views.booking_status,
         name='booking_status'),
    path('dashboard/availability/add/', platform_views.availability_add,
         name='availability_add'),
    path('dashboard/availability/<int:pk>/delete/', platform_views.availability_delete,
         name='availability_delete'),
    path('dashboard/reports/', platform_views.reports, name='reports'),
    path('dashboard/knowledge/', platform_views.knowledge_list, name='knowledge'),
    path('dashboard/knowledge/save/', platform_views.knowledge_save, name='knowledge_save'),
    path('dashboard/knowledge/<int:pk>/action/', platform_views.knowledge_action,
         name='knowledge_action'),
    path('dashboard/knowledge/test/', platform_views.knowledge_test, name='knowledge_test'),
    path('dashboard/users/', platform_views.users_list, name='dash_users'),
    path('dashboard/users/invite/', platform_views.user_invite, name='user_invite'),
    path('dashboard/users/<int:pk>/remove/', platform_views.user_remove, name='user_remove'),
    path('dashboard/audit/', platform_views.audit_list, name='audit'),
    path('dashboard/clients/', views.clients_list, name='clients_list'),
    path('dashboard/clients/new/', views.client_form, name='client_create'),
    path('dashboard/clients/<int:pk>/', views.client_form, name='client_edit'),
    path('dashboard/clients/<int:pk>/toggle/', views.client_toggle_active, name='client_toggle_active'),
    path('dashboard/clients/<int:pk>/resend/', views.client_resend_onboarding, name='client_resend_onboarding'),
    path('dashboard/clients/<int:pk>/live/', views.client_mark_live, name='client_mark_live'),

    # Client self-service (scoped to the member's own organization)
    path('dashboard/instalar/', views.install, name='install'),
    path('dashboard/facturacion/', views.billing, name='billing'),
    path('dashboard/facturacion/portal/', views.billing_portal, name='billing_portal'),

    path('dashboard/password/', views.password_change, name='password_change'),
    path('dashboard/password/reset/', password_reset_view, name='password_reset'),
    path('dashboard/password/reset/done/',
         auth_views.PasswordResetDoneView.as_view(
             template_name='landing/dashboard_password_reset_done.html'),
         name='password_reset_done'),
    path('dashboard/password/reset/<uidb64>/<token>/',
         views.DashboardPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('dashboard/password/reset/complete/',
         auth_views.PasswordResetCompleteView.as_view(
             template_name='landing/dashboard_password_reset_complete.html'),
         name='password_reset_complete'),
    path('dashboard/login/', login_view, name='login'),
    path('dashboard/logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
]

# Dev-only browser auto-reload — never registered in production.
if settings.DEBUG:
    urlpatterns.insert(1, path('__reload__/', include('django_browser_reload.urls')))
