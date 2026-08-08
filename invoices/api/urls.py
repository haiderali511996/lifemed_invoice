"""Mobile API routes, versioned so the app can be upgraded independently.

An MR out in the field will not update the app the day a new version ships, so
/api/v1/ has to keep working after the server has moved on.
"""

from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path('auth/login/', views.login, name='login'),
    path('auth/logout/', views.logout, name='logout'),

    path('bootstrap/', views.bootstrap, name='bootstrap'),

    path('schedule/', views.schedule, name='schedule'),
    path('schedule/generate/', views.create_schedule, name='create_schedule'),

    path('call-points/', views.call_points, name='call_points'),

    path('doctors/', views.doctors, name='doctors'),
    path('doctors/<int:doctor_id>/', views.update_doctor, name='update_doctor'),
    path('doctors/<int:doctor_id>/move/', views.move_doctor, name='move_doctor'),

    path('visits/', views.visits, name='visits'),

    path('performance/', views.performance, name='performance'),
    path('expenses/', views.expenses, name='expenses'),
]
