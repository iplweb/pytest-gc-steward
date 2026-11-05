"""Django test client responses form a reference cycle via ``response.json``.

Run: ``python django_test_client_cycle_repro.py`` (needs Django installed).
Expected output: 71 objects in cycles after one request, 0 once
``response.json`` is removed.
"""

import gc

import django
from django.conf import settings

settings.configure(
    ROOT_URLCONF=__name__,
    ALLOWED_HOSTS=["testserver"],
    SECRET_KEY="x",
    TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
)
django.setup()

from django.http import HttpResponse  # noqa: E402
from django.template import engines  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import path  # noqa: E402


def view(request):
    tpl = engines["django"].from_string("{% for i in r %}<p>{{ i }}</p>{% endfor %}")
    return HttpResponse(tpl.render({"r": range(50)}))


urlpatterns = [path("", view)]
setup_test_environment()

c = Client()
c.get("/")
gc.disable()
gc.collect()

r = c.get("/")
del r
print("objects in cycles after one request:", gc.collect())

r = c.get("/")
del r.json
del r
print("same, after removing response.json:", gc.collect())
