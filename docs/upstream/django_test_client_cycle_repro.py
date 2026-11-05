"""Minimal: django.test.Client response tworzy cykl referencji (response.json = partial(..., response))."""
import gc, sys, django
from django.conf import settings
settings.configure(
    DEBUG=False, ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"], SECRET_KEY="x",
    TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
    INSTALLED_APPS=[], MIDDLEWARE=[],
)
django.setup()
from django.http import HttpResponse
from django.template import engines
from django.urls import path
from django.test import Client
from django.test.utils import setup_test_environment

def view(request):
    return HttpResponse(engines["django"].from_string("{% for i in r %}<p>{{ i }}</p>{% endfor %}").render({"r": range(50)}))

urlpatterns = [path("", view)]
setup_test_environment()  # jak test runner / pytest-django: instrumentuje template_rendered
print("Python", sys.version.split()[0], "Django", django.get_version())
c = Client()
c.get("/")
gc.disable(); gc.collect()
r = c.get("/"); del r
print("po jednym c.get('/') obiektow w cyklach:", gc.collect())
gc.set_debug(gc.DEBUG_SAVEALL)
r = c.get("/"); del r
gc.collect(); g = gc.garbage[:]; gc.set_debug(0); gc.garbage.clear()
from functools import partial
from django.http import HttpResponseBase
resp = [o for o in g if isinstance(o, HttpResponseBase)]
parts = [o for o in g if isinstance(o, partial)]
print("w smieciach: HttpResponse:", len(resp), " partial:", len(parts),
      " partial->response:", any(any(a is resp[0] for a in p.args) for p in parts) if resp else None)

r = c.get("/"); del r.json; del r
print("po usunieciu response.json przed del: obiektow w cyklach:", gc.collect())
r = c.get("/"); r.context; r.resolver_match; del r
print("z dostepem do context/resolver_match:", gc.collect())
