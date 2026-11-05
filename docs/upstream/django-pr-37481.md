#### Trac ticket number

ticket-37481

#### Branch description

`Client.request()` and `AsyncClient.request()` set `response.json = partial(self._parse_json, response)`. The partial holds a strong reference to the response and lives on the response, so every test-client response sits in a reference cycle. It, together with `templates`, `context`, the request and the content, is only freed when the cyclic GC runs. This change binds the helper to `weakref.proxy(response)` instead. `response.json()` behaves the same, including the cached `_json` (#27516), and a response is now freed as soon as it is deleted.

Tests: `test_response_not_in_reference_cycle` for `Client` and `AsyncClient`. With the automatic GC disabled, each takes a `weakref.ref` to a response, deletes the response and asserts the ref is dead. The async test yields to the event loop once (`await asyncio.sleep(0)`), because the finished future that delivered the response is still referenced by the pending task wake-up. That is not a cycle. Both tests fail without the change. `test_client`, `test_client_regress`, `test_utils`, `view_tests`, `generic_views`, `admin_views` and `auth_tests` pass (2352 tests).

One behaviour change: a `json` callable detached from a response that has since been freed raises `ReferenceError` (e.g. `f = client.get(...).json; f()`). A `copy.copy()` of a response shares the helper bound to the original, as it did before.

#### AI Assistance Disclosure (REQUIRED)
- [ ] **No AI tools were used** in preparing this PR.
- [x] **If AI tools were used**, I have disclosed which ones, and fully reviewed and verified their output.

Claude Code (Claude Opus 5.5) was used to investigate the memory growth, write the reproducer, the change and the tests.

#### Checklist
- [x] This PR follows the [contribution guidelines](https://docs.djangoproject.com/en/stable/internals/contributing/writing-code/submitting-patches/).
- [x] This PR **does not** disclose a security vulnerability (see [vulnerability reporting](https://docs.djangoproject.com/en/stable/internals/security/)).
- [x] This PR targets the `main` branch.
- [x] The commit message is written in past tense, mentions the ticket number (if applicable), and ends with a period (see [guidelines](https://docs.djangoproject.com/en/dev/internals/contributing/committing-code/#committing-guidelines)).
- [x] I have not requested, and will not request, an automated AI review for this PR.

- [x] I have checked the "Has patch" ticket flag in the Trac system.
- [x] I have added or updated relevant tests.
- [ ] I have added or updated relevant docs, including release notes if applicable.
- [ ] I have attached screenshots in both light and dark modes for any UI changes.
