# Zgłoszenie do Django Trac — do wklejenia

Formularz: https://code.djangoproject.com/newticket

| Pole | Wartość |
|---|---|
| Summary | Test client responses form a reference cycle via `response.json` and need a GC pass to be freed |
| Type | Cleanup/optimization |
| Component | Testing framework |
| Version | dev |
| Severity | Normal |
| Keywords | gc, reference cycle, memory, test client |
| Has patch | (zostaw puste, dopóki nie ma PR-a) |

---

## Description (Trac wiki markup — wklej od tej linii)

`Client.request()` and `AsyncClient.request()` attach a `json` helper to every
response as

{{{#!python
response.json = partial(self._parse_json, response)
}}}

([https://github.com/django/django/blob/main/django/test/client.py#L1132 client.py#L1132],
[https://github.com/django/django/blob/main/django/test/client.py#L1524 #L1524]).

The `partial` holds a strong reference to the response and is stored on the
response itself, so every response returned by the test client is part of a
reference cycle. Reference counting can never free it; it survives until the
cyclic garbage collector runs. Because the response also holds
`response.templates`, `response.context` (every `Context`/`RequestContext`
rendered during the request), `response.request`, `response.wsgi_request` and
the rendered content, the whole graph stays alive with it.

This is the same class of problem as #36701 (`ModelState` cycles), which was
accepted and fixed.

=== Impact ===

On a large suite (~11k tests, many of them making admin requests) this was the
main source of cyclic garbage. With Python 3.14.0–3.14.4 (incremental GC,
reverted in 3.14.5) the garbage was collected much more slowly than it was
produced: a single pytest-xdist worker grew from ~360 MB to ~1.8 GB over 1266
tests, and `-n auto` on a 14-core / 14 GB machine exhausted RAM. With the
generational GC the garbage does get collected, but every test-client response
still has to be reclaimed by a gen-2 pass instead of being freed on `del`,
which means extra GC work and a higher memory peak in long suites.

=== Reproduction (standalone, no project needed) ===

{{{#!python
import gc
import django
from django.conf import settings

settings.configure(
    ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"], SECRET_KEY="x",
    TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
)
django.setup()

from django.http import HttpResponse
from django.template import engines
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import path


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
print("objects in cycles after one request:", gc.collect())  # 71

r = c.get("/")
del r.json
del r
print("same, after removing response.json:", gc.collect())  # 0
}}}

Output (Django 5.2.19 and main, Python 3.14.9; same on 3.14.4):

{{{
objects in cycles after one request: 71
same, after removing response.json: 0
}}}

`response.json` is the only cycle. Once it is removed, nothing is left for the
collector.

=== Possible fix ===

Keep a weak reference to the response in the helper:

{{{#!python
response.json = partial(self._parse_json, weakref.proxy(response))
}}}

(in both `Client.request()` and `AsyncClient.request()`). `response.json()`
keeps working, and the parsed result is still cached on `response._json`
(#27516). With this change the reproduction above reports `0` objects in cycles.
The only behaviour change is that calling a detached `json` after the response
itself has been released raises `ReferenceError`, e.g.
`f = client.get(...).json; f()`. That seems unlikely in practice. If it matters,
`json` could instead be exposed through a small descriptor.

A regression test could assert that, with `gc.disable()`, a response from
`client.get()` is freed as soon as it is deleted. For example, take a
`weakref.ref(response)`, then `del response`, and check that the ref is `None`
without calling `gc.collect()`.
