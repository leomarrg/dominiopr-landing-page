from django.shortcuts import render

def index(request):
    return render(request, 'landing/index.html')

def terms(request):
    return render(request, 'landing/terms.html')

def privacy(request):
    return render(request, 'landing/privacy.html')
