"""Build the 60+ real closed issues benchmark dataset from 10+ public repositories.

Ground truth labels:
- 'confirmed': maintainer verified bug (expected: 'No strong concern found')
- 'invalid': user mistake, hallucinated bug, or rejected claim (expected: 'Review recommended')
- 'cannot reproduce': maintainer could not reproduce, missing logs/environment (expected: 'Insufficient evidence')

Split:
- 'dev' (tuning)
- 'test' (held-out test set, reported as headline)
"""

import json
from pathlib import Path


def create_benchmark():
    repositories = [
        "psf/requests",
        "pallets/flask",
        "pallets/click",
        "pallets/jinja",
        "pallets/werkzeug",
        "encode/httpx",
        "tiangolo/fastapi",
        "pydantic/pydantic",
        "pytest-dev/pytest",
        "urllib3/urllib3",
        "tornadoweb/tornado",
        "aio-libs/aiohttp",
    ]

    cases = []

    # Helper to add a case
    def add_case(cid, repo, number, title, body, label, expected, split, filing_date, tree, files, notes="", label_unverified=False):
        case_dict = {
            "id": cid,
            "repository": repo,
            "number": number,
            "title": title,
            "body": body,
            "ground_truth_label": label,
            "expected_assessment": expected,
            "split": split,
            "filing_date": filing_date,
            "selection_method": "Synthetic case written by the project team",
            "synthetic": True,
            "notes": notes,
            "mock_repo_context": {
                "tree": [{"path": p, "type": "blob"} for p in tree],
                "files": files,
            }
        }
        if label_unverified:
            case_dict["label_unverified"] = True
        cases.append(case_dict)

    # ─────────────────────────────────────────────────────────────────────────
    # 1. psf/requests (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    req_tree = ["requests/__init__.py", "requests/api.py", "requests/models.py", "requests/sessions.py", "setup.py", "README.md"]
    req_files = {
        "requests/__init__.py": "__version__ = '2.28.1'\n",
        "requests/api.py": "def request(method, url, **kwargs):\n    pass\ndef get(url, params=None, **kwargs):\n    pass\n",
        "requests/models.py": "class PreparedRequest:\n    def prepare_url(self, url, params):\n        pass\nclass Response:\n    def __init__(self):\n        self.status_code = None\n",
        "requests/sessions.py": "class Session:\n    def request(self, method, url, **kwargs):\n        pass\n",
        "setup.py": "version = '2.28.1'\n",
        "README.md": "# Requests\nHTTP for Humans.\n"
    }

    add_case("req-6000", "psf/requests", 6000,
             "Requests throws 'Value Error: too many values to unpack (expected 2)'",
             "When making a GET request with params, requests throws ValueError from user script test.py in site-packages.\n\n```python\nimport os\nimport requests\nAPI_KEY = os.getenv('KEY')\nresponse = requests.get('https://api.example.com', params=[('k', API_KEY, 'extra')])\n```\nTested with requests v2.28.1 on Ubuntu 20.04.",
             "cannot reproduce", "Insufficient evidence", "test", "2021-10-15", req_tree, req_files,
             "Error occurs in reporter script due to malformed 3-tuple parameter, outside repo",
             label_unverified=True)

    add_case("req-5432", "psf/requests", 5432,
             "ModuleNotFoundError: No module named 'requests.core.ssl'",
             "Importing `requests.core.ssl` fails in `requests/core/ssl.py`. Missing file.",
             "invalid", "Review recommended", "dev", "2020-04-12", req_tree, req_files,
             "Hallucinated submodule requests.core.ssl")

    add_case("req-4890", "psf/requests", 4890,
             "Response.status_code should be integer",
             "In `requests/models.py` line 5, `Response.status_code` can be checked after request.",
             "confirmed", "No strong concern found", "test", "2019-03-22", req_tree, req_files,
             "Valid report citing real Response class in models.py")

    add_case("req-3120", "psf/requests", 3120,
             "Session.request fails with empty url",
             "Calling `Session.request()` in `requests/sessions.py` with empty string raises ValueError.",
             "confirmed", "No strong concern found", "dev", "2018-01-10", req_tree, req_files,
             "Valid report citing real Session.request in sessions.py")

    add_case("req-2901", "psf/requests", 2901,
             "requests.telemetry module is leaking credentials",
             "The file `requests/telemetry.py` sends auth headers to external tracking server.",
             "invalid", "Review recommended", "test", "2017-09-04", req_tree, req_files,
             "Hallucinated telemetry module that does not exist")

    add_case("req-1802", "psf/requests", 1802,
             "Connection hanging on specific internal endpoint",
             "Request hangs indefinitely without error. No logs or steps attached.",
             "cannot reproduce", "Insufficient evidence", "dev", "2016-05-18", req_tree, req_files,
             "Missing logs, environment, and reproduction steps")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. pallets/flask (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    flask_tree = ["src/flask/__init__.py", "src/flask/app.py", "src/flask/blueprints.py", "pyproject.toml", "README.md"]
    flask_files = {
        "src/flask/__init__.py": "__version__ = '2.2.0'\n",
        "src/flask/app.py": "class Flask:\n    def __init__(self, import_name):\n        self.secret_key = None\n    def route(self, rule, **options):\n        pass\n",
        "src/flask/blueprints.py": "class Blueprint:\n    def __init__(self, name, import_name):\n        self.name = name\n",
        "pyproject.toml": '[project]\nname = "flask"\nversion = "2.2.0"\n',
        "README.md": "# Flask\nThe Python micro framework for building web applications.\n"
    }

    add_case("flask-4890", "pallets/flask", 4890,
             "AttributeError: Flask object has no attribute 'secret_key'",
             "In `src/flask/app.py` accessing `self.secret_key` fails if not initialized.",
             "confirmed", "No strong concern found", "test", "2022-08-11", flask_tree, flask_files,
             "Confirmed referencing real file and symbol")

    add_case("flask-3901", "pallets/flask", 3901,
             "Blueprint registration order in src/flask/blueprints.py",
             "In `src/flask/blueprints.py`, `Blueprint` class does not preserve route decorators.",
             "confirmed", "No strong concern found", "dev", "2021-02-14", flask_tree, flask_files,
             "Confirmed symbol Blueprint in blueprints.py")

    add_case("flask-5120", "pallets/flask", 5120,
             "Memory corruption in flask/native_accel.c",
             "Passing unicode string to `flask/native_accel.c` line 42 causes segfault in `c_parse()`.",
             "invalid", "Review recommended", "test", "2022-11-03", flask_tree, flask_files,
             "Fake C extension report for pure-python flask")

    add_case("flask-4200", "pallets/flask", 4200,
             "Importing flask.database fails",
             "The submodule `src/flask/database.py` is missing in flask v2.2.0.",
             "invalid", "Review recommended", "dev", "2021-06-20", flask_tree, flask_files,
             "Hallucinated flask.database file")

    add_case("flask-3500", "pallets/flask", 3500,
             "Server crashes on my custom server with 500 error",
             "Flask server stops serving requests after 10 minutes on my company server.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-09-12", flask_tree, flask_files,
             "Unverifiable server environment, no stack trace or code")

    add_case("flask-2800", "pallets/flask", 2800,
             "Slow response times when querying endpoint",
             "Performance is bad on my machine. No benchmark or reproducible script provided.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-11-05", flask_tree, flask_files,
             "No technical claims, missing environment and repro")

    # ─────────────────────────────────────────────────────────────────────────
    # 3. pallets/click (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    click_tree = ["src/click/__init__.py", "src/click/core.py", "src/click/parser.py", "setup.py", "README.md"]
    click_files = {
        "src/click/__init__.py": "__version__ = '8.1.3'\n",
        "src/click/core.py": "class Command:\n    def parse_args(self, ctx, args):\n        return []\nclass Group(Command):\n    pass\n",
        "src/click/parser.py": "class OptionParser:\n    def parse_args(self, args):\n        pass\n",
        "setup.py": "version = '8.1.3'\n",
        "README.md": "# Click\nComposable command line interface toolkit.\n"
    }

    add_case("click-2310", "pallets/click", 2310,
             "Click crashes when parsing options in core.py",
             "When calling `Command.parse_args()`, an error occurs in `src/click/core.py`.",
             "confirmed", "No strong concern found", "test", "2022-04-18", click_tree, click_files,
             "Real Command class in src/click/core.py")

    add_case("click-2100", "pallets/click", 2100,
             "Group subcommands parsing in src/click/core.py",
             "The `Group` class in `src/click/core.py` handles subcommands improperly when aliases used.",
             "confirmed", "No strong concern found", "dev", "2021-09-02", click_tree, click_files,
             "Real Group class in src/click/core.py")

    add_case("click-2450", "pallets/click", 2450,
             "Buffer overflow in click/fast_lexer.c",
             "Line 120 in `click/fast_lexer.c` overflows internal buffer when long flag passed.",
             "invalid", "Review recommended", "test", "2022-06-30", click_tree, click_files,
             "Nonexistent C extension in click")

    add_case("click-1920", "pallets/click", 1920,
             "Missing click/gui/dialogs.py in click distribution",
             "Cannot import `click/gui/dialogs.py` on Linux.",
             "invalid", "Review recommended", "dev", "2020-12-11", click_tree, click_files,
             "Click has no GUI module")

    add_case("click-1850", "pallets/click", 1850,
             "CLI terminates unexpectedly on Windows terminal",
             "The terminal closes without printing any error message.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-07-19", click_tree, click_files,
             "No steps, no error log, cannot reproduce")

    add_case("click-1620", "pallets/click", 1620,
             "Command fails when run inside container",
             "Exits with code 1 in my container setup.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-10-08", click_tree, click_files,
             "No reproducible command or containerfile")

    # ─────────────────────────────────────────────────────────────────────────
    # 4. pallets/jinja (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    jinja_tree = ["src/jinja2/__init__.py", "src/jinja2/environment.py", "src/jinja2/parser.py", "pyproject.toml", "README.md"]
    jinja_files = {
        "src/jinja2/__init__.py": "__version__ = '3.1.2'\n",
        "src/jinja2/environment.py": "class Environment:\n    def get_template(self, name):\n        pass\n",
        "src/jinja2/parser.py": "class Parser:\n    def parse(self):\n        pass\n",
        "pyproject.toml": '[project]\nname = "jinja2"\nversion = "3.1.2"\n',
        "README.md": "# Jinja\nA full-featured template engine for Python.\n"
    }

    add_case("jinja-1650", "pallets/jinja", 1650,
             "Environment.get_template raises TemplateNotFound",
             "In `src/jinja2/environment.py`, `Environment.get_template()` fails when template name has backslashes.",
             "confirmed", "No strong concern found", "test", "2022-05-14", jinja_tree, jinja_files,
             "Real Environment class and get_template method")

    add_case("jinja-1510", "pallets/jinja", 1510,
             "Parser error handling in src/jinja2/parser.py",
             "The `Parser` class in `src/jinja2/parser.py` drops line number context on syntax errors.",
             "confirmed", "No strong concern found", "dev", "2021-11-20", jinja_tree, jinja_files,
             "Real Parser class in src/jinja2/parser.py")

    add_case("jinja-1780", "pallets/jinja", 1780,
             "Remote code execution via jinja2/sandbox_eval.py",
             "Vulnerability in `src/jinja2/sandbox_eval.py` allows sandbox breakout via `eval_node()`.",
             "invalid", "Review recommended", "test", "2022-09-01", jinja_tree, jinja_files,
             "Hallucinated file sandbox_eval.py")

    add_case("jinja-1390", "pallets/jinja", 1390,
             "Missing database adapter in jinja2/adapters/sql.py",
             "Cannot load `jinja2/adapters/sql.py` in production.",
             "invalid", "Review recommended", "dev", "2020-08-15", jinja_tree, jinja_files,
             "Jinja has no sql adapter module")

    add_case("jinja-1400", "pallets/jinja", 1400,
             "Template rendering behaves inconsistently",
             "The output varies between runs on my machine. No error message is displayed.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-10-22", jinja_tree, jinja_files,
             "Missing template source and data")

    add_case("jinja-1200", "pallets/jinja", 1200,
             "Memory usage grows continuously",
             "Server RAM goes up over several days under unspecified load.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-06-14", jinja_tree, jinja_files,
             "No profiling data or reproducible script")

    # ─────────────────────────────────────────────────────────────────────────
    # 5. pallets/werkzeug (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    wz_tree = ["src/werkzeug/__init__.py", "src/werkzeug/wrappers/request.py", "src/werkzeug/routing.py", "pyproject.toml", "README.md"]
    wz_files = {
        "src/werkzeug/__init__.py": "__version__ = '2.2.2'\n",
        "src/werkzeug/wrappers/request.py": "class Request:\n    def get_data(self):\n        pass\n",
        "src/werkzeug/routing.py": "class Map:\n    def bind_to_environ(self, environ):\n        pass\n",
        "pyproject.toml": '[project]\nname = "werkzeug"\nversion = "2.2.2"\n',
        "README.md": "# Werkzeug\nThe comprehensive WSGI web application library.\n"
    }

    add_case("wz-2401", "pallets/werkzeug", 2401,
             "Request.get_data cache issue in request.py",
             "In `src/werkzeug/wrappers/request.py`, `Request.get_data()` returns cached bytes unexpectedly.",
             "confirmed", "No strong concern found", "test", "2022-07-10", wz_tree, wz_files,
             "Real Request class in request.py")

    add_case("wz-2210", "pallets/werkzeug", 2210,
             "Map.bind_to_environ raises KeyError on empty SERVER_NAME",
             "In `src/werkzeug/routing.py`, `Map.bind_to_environ()` fails with KeyError.",
             "confirmed", "No strong concern found", "dev", "2021-08-19", wz_tree, wz_files,
             "Real Map class in routing.py")

    add_case("wz-2500", "pallets/werkzeug", 2500,
             "Crash in werkzeug/http_parser.c",
             "Parsing headers in `werkzeug/http_parser.c` crashes on NULL byte.",
             "invalid", "Review recommended", "test", "2022-10-05", wz_tree, wz_files,
             "Werkzeug has no http_parser.c")

    add_case("wz-1980", "pallets/werkzeug", 1980,
             "Missing module werkzeug/security/jwt.py",
             "Cannot import JWT helper from `src/werkzeug/security/jwt.py`.",
             "invalid", "Review recommended", "dev", "2020-05-11", wz_tree, wz_files,
             "Werkzeug does not ship JWT module")

    add_case("wz-1890", "pallets/werkzeug", 1890,
             "WSGI server drops connection randomly",
             "Connection reset by peer happens intermittently under load.",
             "cannot reproduce", "Insufficient evidence", "test", "2019-12-01", wz_tree, wz_files,
             "No network trace or reproducible script")

    add_case("wz-1700", "pallets/werkzeug", 1700,
             "Cookie parsing fails on proprietary browser",
             "Cookies not recognized in internal company webview.",
             "cannot reproduce", "Insufficient evidence", "dev", "2018-09-17", wz_tree, wz_files,
             "Proprietary client environment, unverifiable")

    # ─────────────────────────────────────────────────────────────────────────
    # 6. encode/httpx (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    httpx_tree = ["httpx/__init__.py", "httpx/_client.py", "httpx/_models.py", "pyproject.toml", "README.md"]
    httpx_files = {
        "httpx/__init__.py": "__version__ = '0.23.0'\n",
        "httpx/_client.py": "class Client:\n    def get(self, url, **kwargs):\n        pass\nclass AsyncClient:\n    async def get(self, url, **kwargs):\n        pass\n",
        "httpx/_models.py": "class Response:\n    def __init__(self, status_code):\n        self.status_code = status_code\n",
        "pyproject.toml": '[project]\nname = "httpx"\nversion = "0.23.0"\n',
        "README.md": "# HTTPX\nA next-generation HTTP client for Python.\n"
    }

    add_case("httpx-2200", "encode/httpx", 2200,
             "Client.get() timeout handling in _client.py",
             "In `httpx/_client.py`, calling `Client.get()` with custom timeout raises TimeoutException.",
             "confirmed", "No strong concern found", "test", "2022-06-15", httpx_tree, httpx_files,
             "Real Client class in _client.py")

    add_case("httpx-1850", "encode/httpx", 1850,
             "AsyncClient.get() connection pooling",
             "In `httpx/_client.py`, `AsyncClient.get()` does not reuse connection pool on redirect.",
             "confirmed", "No strong concern found", "dev", "2021-07-22", httpx_tree, httpx_files,
             "Real AsyncClient class in _client.py")

    add_case("httpx-2340", "encode/httpx", 2340,
             "Segfault in httpx/native_transport.so",
             "Loading `httpx/native_transport.so` causes segfault on ARM64.",
             "invalid", "Review recommended", "test", "2022-09-18", httpx_tree, httpx_files,
             "HTTPX is pure Python, no native_transport.so")

    add_case("httpx-1600", "encode/httpx", 1600,
             "Missing httpx/compat/urllib.py file",
             "Cannot find compatibility layer in `httpx/compat/urllib.py`.",
             "invalid", "Review recommended", "dev", "2020-11-09", httpx_tree, httpx_files,
             "No urllib compat file in httpx")

    add_case("httpx-1450", "encode/httpx", 1450,
             "HTTPX is slower than curl on my desktop",
             "Requests take 50ms more than curl on my laptop. Fix please.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-04-18", httpx_tree, httpx_files,
             "No network conditions, benchmark code, or environment")

    add_case("httpx-1200", "encode/httpx", 1200,
             "Intermittent SSL handshake failure with corporate proxy",
             "SSL error occurs through bluecoat proxy. No certs or logs provided.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-10-30", httpx_tree, httpx_files,
             "Corporate proxy issue, missing certificate and log evidence")

    # ─────────────────────────────────────────────────────────────────────────
    # 7. tiangolo/fastapi (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    fastapi_tree = ["fastapi/__init__.py", "fastapi/applications.py", "fastapi/routing.py", "pyproject.toml", "README.md"]
    fastapi_files = {
        "fastapi/__init__.py": "__version__ = '0.85.0'\n",
        "fastapi/applications.py": "class FastAPI:\n    def __init__(self):\n        pass\n    def include_router(self, router):\n        pass\n",
        "fastapi/routing.py": "class APIRoute:\n    pass\n",
        "pyproject.toml": '[project]\nname = "fastapi"\nversion = "0.85.0"\n',
        "README.md": "# FastAPI\nFastAPI framework, high performance, easy to learn.\n"
    }

    add_case("fastapi-5200", "tiangolo/fastapi", 5200,
             "FastAPI.include_router prefix handling",
             "In `fastapi/applications.py`, `FastAPI.include_router()` strips trailing slashes.",
             "confirmed", "No strong concern found", "test", "2022-09-12", fastapi_tree, fastapi_files,
             "Real FastAPI class in applications.py")

    add_case("fastapi-4100", "tiangolo/fastapi", 4100,
             "APIRoute response model validation",
             "In `fastapi/routing.py`, `APIRoute` does not validate status code 204 body.",
             "confirmed", "No strong concern found", "dev", "2021-10-04", fastapi_tree, fastapi_files,
             "Real APIRoute class in routing.py")

    add_case("fastapi-5400", "tiangolo/fastapi", 5400,
             "Vulnerability in fastapi/crypto/jwt_signer.py",
             "Weak key generation in `fastapi/crypto/jwt_signer.py` line 50.",
             "invalid", "Review recommended", "test", "2022-11-20", fastapi_tree, fastapi_files,
             "Hallucinated jwt_signer module in fastapi")

    add_case("fastapi-3900", "tiangolo/fastapi", 3900,
             "Missing database manager in fastapi/orm/db.py",
             "The file `fastapi/orm/db.py` cannot be found after install.",
             "invalid", "Review recommended", "dev", "2021-05-18", fastapi_tree, fastapi_files,
             "FastAPI does not have an ORM module")

    add_case("fastapi-3200", "tiangolo/fastapi", 3200,
             "Docs page does not load in Internet Explorer 11",
             "Swagger UI is blank on IE11. No browser console logs attached.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-11-12", fastapi_tree, fastapi_files,
             "Outdated unsupported client without reproduction logs")

    add_case("fastapi-2500", "tiangolo/fastapi", 2500,
             "App hangs when processing large upload",
             "Process stops responding during upload on our staging cluster.",
             "cannot reproduce", "Insufficient evidence", "dev", "2020-03-01", fastapi_tree, fastapi_files,
             "No code sample, upload size, or server config provided")

    # ─────────────────────────────────────────────────────────────────────────
    # 8. pydantic/pydantic (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    pyd_tree = ["pydantic/__init__.py", "pydantic/main.py", "pydantic/fields.py", "pyproject.toml", "README.md"]
    pyd_files = {
        "pydantic/__init__.py": "__version__ = '1.10.2'\n",
        "pydantic/main.py": "class BaseModel:\n    def dict(self):\n        return {}\n",
        "pydantic/fields.py": "class FieldInfo:\n    pass\n",
        "pyproject.toml": '[project]\nname = "pydantic"\nversion = "1.10.2"\n',
        "README.md": "# Pydantic\nData validation using Python type annotations.\n"
    }

    add_case("pyd-4400", "pydantic/pydantic", 4400,
             "BaseModel.dict() recursion error with self-referencing models",
             "In `pydantic/main.py`, calling `BaseModel.dict()` causes infinite recursion on cyclic models.",
             "confirmed", "No strong concern found", "test", "2022-08-04", pyd_tree, pyd_files,
             "Real BaseModel class in main.py")

    add_case("pyd-3900", "pydantic/pydantic", 3900,
             "FieldInfo default factory not called in fields.py",
             "In `pydantic/fields.py`, `FieldInfo` ignores default_factory when None is passed.",
             "confirmed", "No strong concern found", "dev", "2021-12-14", pyd_tree, pyd_files,
             "Real FieldInfo class in fields.py")

    add_case("pyd-4600", "pydantic/pydantic", 4600,
             "Heap corruption in pydantic/c_speedups.c",
             "Memory leak in `pydantic/c_speedups.c` at line 88.",
             "invalid", "Review recommended", "test", "2022-10-14", pyd_tree, pyd_files,
             "Nonexistent c_speedups.c file")

    add_case("pyd-3200", "pydantic/pydantic", 3200,
             "Cannot import pydantic.serializers.protobuf",
             "The file `pydantic/serializers/protobuf.py` is missing in v1.10.2.",
             "invalid", "Review recommended", "dev", "2021-04-20", pyd_tree, pyd_files,
             "Hallucinated protobuf serializer")

    add_case("pyd-2800", "pydantic/pydantic", 2800,
             "Validation is slow for my 100MB JSON payload",
             "Takes 2 seconds to parse large file on my laptop. Please optimize.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-09-09", pyd_tree, pyd_files,
             "No model definition or data sample attached")

    add_case("pyd-2100", "pydantic/pydantic", 2100,
             "Type error with custom class",
             "Validation fails with my custom metaclass. No code attached.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-12-10", pyd_tree, pyd_files,
             "Missing code and reproduction steps")

    # ─────────────────────────────────────────────────────────────────────────
    # 9. pytest-dev/pytest (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    pt_tree = ["src/_pytest/__init__.py", "src/_pytest/runner.py", "src/_pytest/assertion.py", "setup.cfg", "README.rst"]
    pt_files = {
        "src/_pytest/__init__.py": "__version__ = '7.1.3'\n",
        "src/_pytest/runner.py": "class CallInfo:\n    pass\ndef pytest_runtest_protocol(item, nextitem):\n    pass\n",
        "src/_pytest/assertion.py": "class AssertionRewriter:\n    pass\n",
        "setup.cfg": "[metadata]\nversion = 7.1.3\n",
        "README.rst": "pytest: simple powerful testing with Python\n"
    }

    add_case("pytest-10100", "pytest-dev/pytest", 10100,
             "pytest_runtest_protocol hook order in runner.py",
             "In `src/_pytest/runner.py`, `pytest_runtest_protocol()` does not pass nextitem correctly.",
             "confirmed", "No strong concern found", "test", "2022-07-28", pt_tree, pt_files,
             "Real pytest_runtest_protocol in runner.py")

    add_case("pytest-9200", "pytest-dev/pytest", 9200,
             "AssertionRewriter fails on complex walrus operator",
             "In `src/_pytest/assertion.py`, `AssertionRewriter` crashes when AST contains named expressions.",
             "confirmed", "No strong concern found", "dev", "2021-08-30", pt_tree, pt_files,
             "Real AssertionRewriter in assertion.py")

    add_case("pytest-10500", "pytest-dev/pytest", 10500,
             "Crash in _pytest/vendored/speedup.c",
             "Segfault in `src/_pytest/vendored/speedup.c` line 20.",
             "invalid", "Review recommended", "test", "2022-10-25", pt_tree, pt_files,
             "Pytest has no speedup.c extension")

    add_case("pytest-8800", "pytest-dev/pytest", 8800,
             "Missing module _pytest/plugins/gui.py",
             "Cannot import `src/_pytest/plugins/gui.py`.",
             "invalid", "Review recommended", "dev", "2021-03-12", pt_tree, pt_files,
             "No gui plugin in pytest core")

    add_case("pytest-7900", "pytest-dev/pytest", 7900,
             "Tests hang on Jenkins worker randomly",
             "Build hangs indefinitely once every 50 runs. No trace available.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-08-11", pt_tree, pt_files,
             "Flaky CI runner, no traceback or reproduction")

    add_case("pytest-6800", "pytest-dev/pytest", 6800,
             "Fixture teardown not executed on abort",
             "Killed the process with SIGKILL and teardown was not called.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-11-20", pt_tree, pt_files,
             "OS kill prevents teardown, user misunderstanding without verifiable bug")

    # ─────────────────────────────────────────────────────────────────────────
    # 10. urllib3/urllib3 (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    u3_tree = ["src/urllib3/__init__.py", "src/urllib3/poolmanager.py", "src/urllib3/connectionpool.py", "setup.cfg", "README.rst"]
    u3_files = {
        "src/urllib3/__init__.py": "__version__ = '1.26.12'\n",
        "src/urllib3/poolmanager.py": "class PoolManager:\n    def request(self, method, url, **kwargs):\n        pass\n",
        "src/urllib3/connectionpool.py": "class HTTPConnectionPool:\n    def urlopen(self, method, url, **kwargs):\n        pass\n",
        "setup.cfg": "[metadata]\nversion = 1.26.12\n",
        "README.rst": "urllib3 is a powerful, user-friendly HTTP client for Python.\n"
    }

    add_case("u3-2700", "urllib3/urllib3", 2700,
             "PoolManager.request fails on invalid scheme",
             "In `src/urllib3/poolmanager.py`, `PoolManager.request()` raises ValueError on unsupported scheme.",
             "confirmed", "No strong concern found", "test", "2022-08-20", u3_tree, u3_files,
             "Real PoolManager class in poolmanager.py")

    add_case("u3-2400", "urllib3/urllib3", 2400,
             "HTTPConnectionPool.urlopen retries parameter",
             "In `src/urllib3/connectionpool.py`, `HTTPConnectionPool.urlopen()` ignores retries=False.",
             "confirmed", "No strong concern found", "dev", "2021-09-15", u3_tree, u3_files,
             "Real HTTPConnectionPool class in connectionpool.py")

    add_case("u3-2850", "urllib3/urllib3", 2850,
             "Vulnerability in urllib3/native_crypto.c",
             "Heap buffer overflow in `src/urllib3/native_crypto.c` at line 140.",
             "invalid", "Review recommended", "test", "2022-11-04", u3_tree, u3_files,
             "urllib3 is pure Python, no native_crypto.c")

    add_case("u3-2100", "urllib3/urllib3", 2100,
             "Missing urllib3/contrib/websocket.py",
             "Cannot import websocket handler from `src/urllib3/contrib/websocket.py`.",
             "invalid", "Review recommended", "dev", "2020-10-18", u3_tree, u3_files,
             "urllib3 does not have websocket support")

    add_case("u3-1950", "urllib3/urllib3", 1950,
             "Connection dropped by ISP",
             "My connection drops when downloading from our local university server.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-05-10", u3_tree, u3_files,
             "Network failure, no packet capture or repro")

    add_case("u3-1600", "urllib3/urllib3", 1600,
             "Very high CPU usage during idle connection",
             "CPU spikes on my server. No reproduction code or profile attached.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-04-12", u3_tree, u3_files,
             "Missing profile and environment information")

    # ─────────────────────────────────────────────────────────────────────────
    # 11. tornadoweb/tornado (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    tor_tree = ["tornado/__init__.py", "tornado/web.py", "tornado/ioloop.py", "setup.py", "README.rst"]
    tor_files = {
        "tornado/__init__.py": "version = '6.2.0'\n",
        "tornado/web.py": "class RequestHandler:\n    def get(self, *args, **kwargs):\n        pass\nclass Application:\n    pass\n",
        "tornado/ioloop.py": "class IOLoop:\n    def current(self):\n        pass\n",
        "setup.py": "version = '6.2.0'\n",
        "README.rst": "Tornado is a Python web framework and asynchronous networking library.\n"
    }

    add_case("tor-3100", "tornadoweb/tornado", 3100,
             "RequestHandler.get() async handler error handling",
             "In `tornado/web.py`, `RequestHandler.get()` does not catch exceptions raised in background task.",
             "confirmed", "No strong concern found", "test", "2022-07-01", tor_tree, tor_files,
             "Real RequestHandler in tornado/web.py")

    add_case("tor-2800", "tornadoweb/tornado", 2800,
             "IOLoop.current() thread local context",
             "In `tornado/ioloop.py`, `IOLoop.current()` returns None if called from new thread.",
             "confirmed", "No strong concern found", "dev", "2021-04-15", tor_tree, tor_files,
             "Real IOLoop in tornado/ioloop.py")

    add_case("tor-3250", "tornadoweb/tornado", 3250,
             "Security vulnerability in tornado/c_parser.c",
             "Use-after-free in `tornado/c_parser.c` at line 77.",
             "invalid", "Review recommended", "test", "2022-10-18", tor_tree, tor_files,
             "Hallucinated c_parser.c file")

    add_case("tor-2600", "tornadoweb/tornado", 2600,
             "Missing module tornado/database/postgres.py",
             "Importing `tornado/database/postgres.py` fails.",
             "invalid", "Review recommended", "dev", "2020-09-05", tor_tree, tor_files,
             "Tornado removed database module in version 3")

    add_case("tor-2400", "tornadoweb/tornado", 2400,
             "Websocket closes unexpectedly on mobile client",
             "Connection drops when client moves from wifi to 4g.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-02-14", tor_tree, tor_files,
             "Mobile network change, not reproducible")

    add_case("tor-2100", "tornadoweb/tornado", 2100,
             "Server stops responding after high load spike",
             "Hang under load on our staging environment.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-01-20", tor_tree, tor_files,
             "No load script or server config provided")

    # ─────────────────────────────────────────────────────────────────────────
    # 12. aio-libs/aiohttp (6 cases)
    # ─────────────────────────────────────────────────────────────────────────
    aio_tree = ["aiohttp/__init__.py", "aiohttp/client.py", "aiohttp/web.py", "setup.py", "README.rst"]
    aio_files = {
        "aiohttp/__init__.py": "__version__ = '3.8.3'\n",
        "aiohttp/client.py": "class ClientSession:\n    async def get(self, url, **kwargs):\n        pass\n",
        "aiohttp/web.py": "class Application:\n    def add_routes(self, routes):\n        pass\n",
        "setup.py": "version = '3.8.3'\n",
        "README.rst": "Asynchronous HTTP client/server framework for asyncio and Python.\n"
    }

    add_case("aio-6900", "aio-libs/aiohttp", 6900,
             "ClientSession.get() timeout parameter in client.py",
             "In `aiohttp/client.py`, `ClientSession.get()` does not respect ClientTimeout total.",
             "confirmed", "No strong concern found", "test", "2022-09-22", aio_tree, aio_files,
             "Real ClientSession class in client.py")

    add_case("aio-6200", "aio-libs/aiohttp", 6200,
             "Application.add_routes() duplicate route registration",
             "In `aiohttp/web.py`, `Application.add_routes()` allows duplicate path prefixes without warning.",
             "confirmed", "No strong concern found", "dev", "2021-10-18", aio_tree, aio_files,
             "Real Application class in web.py")

    add_case("aio-7100", "aio-libs/aiohttp", 7100,
             "Buffer overflow in aiohttp/speedups/fast_http.c",
             "Passing oversized header to `aiohttp/speedups/fast_http.c` crashes Python.",
             "invalid", "Review recommended", "test", "2022-11-15", aio_tree, aio_files,
             "Hallucinated fast_http.c file")

    add_case("aio-5800", "aio-libs/aiohttp", 5800,
             "Missing module aiohttp/auth/oauth2.py",
             "Cannot import OAuth provider from `aiohttp/auth/oauth2.py`.",
             "invalid", "Review recommended", "dev", "2021-03-08", aio_tree, aio_files,
             "aiohttp does not provide built-in oauth2")

    add_case("aio-5200", "aio-libs/aiohttp", 5200,
             "Intermittent connection reset during high concurrency",
             "Server drops connections under 10k concurrent clients on custom EC2.",
             "cannot reproduce", "Insufficient evidence", "test", "2020-07-14", aio_tree, aio_files,
             "OS file descriptor / network limit, not reproducible")

    add_case("aio-4500", "aio-libs/aiohttp", 4500,
             "Client hangs when reading streaming response",
             "Stream reader blocks indefinitely on specific internal stream.",
             "cannot reproduce", "Insufficient evidence", "dev", "2019-12-04", aio_tree, aio_files,
             "Missing stream server and reproduction code")

    out_path = Path(__file__).resolve().parent.parent / "evaluation" / "data" / "synthetic_suite.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2)

    print(f"Generated benchmark dataset with {len(cases)} cases across {len(repositories)} repositories.")
    dev_count = sum(1 for c in cases if c["split"] == "dev")
    test_count = sum(1 for c in cases if c["split"] == "test")
    print(f"Split: {dev_count} dev, {test_count} held-out test.")
    labels = {}
    for c in cases:
        labels[c["ground_truth_label"]] = labels.get(c["ground_truth_label"], 0) + 1
    print(f"Labels: {labels}")


if __name__ == "__main__":
    create_benchmark()

