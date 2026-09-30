"""连接器单测：全部走本地 mock（HTTP 服务 / SQLite / SMTP 桩），不依赖外部服务。"""
import json
import base64
import hashlib
import hmac
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow

import plaita_nodes

plaita_nodes.register_all()  # 生产环境由 console/worker 注册；测试内显式调用


# ---- 工具 ----

@pytest.fixture()
def http_mock():
    """本地 HTTP mock：记录请求，返回可配置响应。"""
    state = {"requests": [], "status": 200, "response": {"ok": True}}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        state["requests"].append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body) if body else None,
            }
        )
        payload = json.dumps(state["response"]).encode()
        self.send_response(state["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    class Handler(BaseHTTPRequestHandler):
        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _cred_env(monkeypatch, tmp_path, name, data):
    """写加密凭据文件并设置环境，返回凭据名。"""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    token = Fernet(key).encrypt(json.dumps(data).encode()).decode()
    cred_file = tmp_path / "creds.json"
    key_file = tmp_path / "creds.key"
    existing = json.loads(cred_file.read_text()) if cred_file.exists() else {}
    existing[name] = {"type": "test", "data": token}
    cred_file.write_text(json.dumps(existing))
    key_file.write_text(key)
    monkeypatch.setenv("PLAITA_CREDENTIALS_FILE", str(cred_file))
    monkeypatch.setenv("PLAITA_CREDENTIALS_KEY_FILE", str(key_file))
    return name


def _run_node(node_type: str, flow_input: dict | None = None, **fields):
    flow = Flow.model_validate({
        "nodes": [
            {"type": "start", "id": "start", "next": "n"},
            {"type": node_type, "id": "n", **fields, "next": "end"},
            {"type": "end", "id": "end", "resultType": "success", "output": "$NODE.n"},
        ]
    })
    return FlowExecution().run_compatible(flow, False, **(flow_input or {}))


# ---- dingtalk_webhook ----

def test_dingtalk_webhook_with_sign(monkeypatch, tmp_path, http_mock):
    state, base = http_mock
    secret = "SECtopsecret"
    _cred_env(monkeypatch, tmp_path, "ding-bot", {"url": f"{base}/robot/send", "secret": secret})

    result = _run_node("dingtalk_webhook", credential="ding-bot", text="构建完成")
    assert result["status"] == 200
    req = state["requests"][0]
    assert req["body"] == {"msgtype": "text", "text": {"content": "构建完成"}}
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(req["path"]).query)
    ts, sign = qs["timestamp"][0], qs["sign"][0]
    expect = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign == expect


# ---- generic_webhook ----

def test_generic_webhook_posts_evaluated_payload(monkeypatch, tmp_path, http_mock):
    state, base = http_mock
    _cred_env(monkeypatch, tmp_path, "hook-1", {"url": f"{base}/cb"})

    result = _run_node("generic_webhook", flow_input={"stage": "prod"}, credential="hook-1",
                       payload={"event": "deploy", "env": "$INPUT.stage"})
    assert result["status"] == 200
    assert state["requests"][0]["body"] == {"event": "deploy", "env": "prod"}


# ---- api_request ----

def test_api_request_builds_request_from_credential(monkeypatch, tmp_path, http_mock):
    state, base = http_mock
    _cred_env(monkeypatch, tmp_path, "gh-api", {
        "base_url": base,
        "headers": {"Authorization": "Bearer tok123"},
    })

    result = _run_node("api_request", flow_input={"org": "acme", "stage": "prod"},
                       credential="gh-api", method="POST", path="repos/$INPUT.org/issues",
                       query={"labels": "bug"},
                       body={"title": "来自 plaita", "env": "$INPUT.stage"})
    assert result["status"] == 200
    req = state["requests"][0]
    assert req["method"] == "POST"
    assert req["path"] == "/repos/acme/issues?labels=bug"
    assert req["headers"]["Authorization"] == "Bearer tok123"
    assert req["body"] == {"title": "来自 plaita", "env": "prod"}


def test_api_request_error_raises(monkeypatch, tmp_path, http_mock):
    state, base = http_mock
    state["status"] = 500
    _cred_env(monkeypatch, tmp_path, "err-api", {"base_url": base})

    with pytest.raises(RuntimeError, match="HTTP 500"):
        _run_node("api_request", credential="err-api", method="GET")

    # raise_on_error=False 时返回原始响应
    result = _run_node("api_request", credential="err-api", method="GET", raise_on_error=False)
    assert result["status"] == 500


# ---- sql_query ----

def test_sql_query_select_and_insert(monkeypatch, tmp_path):
    import sqlalchemy

    db_file = tmp_path / "test.db"
    url = f"sqlite:///{db_file}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE items (id INTEGER, name TEXT)"))
        conn.execute(sqlalchemy.text("INSERT INTO items VALUES (1, 'a'), (2, 'b')"))

    _cred_env(monkeypatch, tmp_path, "main-db", {"url": url})

    rows = _run_node("sql_query", credential="main-db",
                     query="SELECT id, name FROM items WHERE id >= :min ORDER BY id",
                     params={"min": 1})
    assert rows == {"rows": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}], "rowcount": 2}

    written = _run_node("sql_query", credential="main-db",
                        query="INSERT INTO items VALUES (3, 'c')")
    assert written == {"rowcount": 1}


# ---- email_send ----

def test_email_send_via_smtp(monkeypatch, tmp_path):
    _cred_env(monkeypatch, tmp_path, "smtp", {
        "host": "smtp.test", "port": 587,
        "username": "bot@test", "password": "pw", "use_tls": True,
    })
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    result = _run_node("email_send", credential="smtp",
                       to=["a@x.com", "b@x.com"], subject="告警", body="磁盘 90%")
    assert result == {"status": "sent", "to": ["a@x.com", "b@x.com"]}
    assert sent["host"] == "smtp.test" and sent["tls"] is True
    assert sent["msg"]["To"] == "a@x.com, b@x.com"
    assert sent["msg"]["Subject"] == "告警"


# ---- dry-run 契约（AGENTS.md：有副作用的节点一律尊重 globalContext.dry_run）----

_DRY_RUN_NODE_TYPES = [
    "feishu_webhook", "wecom_webhook", "slack_webhook", "dingtalk_webhook",
    "generic_webhook", "api_request", "email_send", "sql_query",
]


@pytest.mark.parametrize("node_type", _DRY_RUN_NODE_TYPES)
def test_connectors_respect_dry_run(node_type):
    """dry_run 下：不解析凭据（无凭据也不报错）、不连网，返回带 dry_run 标记的 fake 结果。"""
    from fake_exec import FakeExecution

    cls = {c.node_type: c for c in plaita_nodes._ALL_NODES}[node_type]
    node = cls(id="n", credential="")  # 故意不配 credential——dry 分支必须先于凭据解析
    out = node.execute(FakeExecution({"dry_run": True}))
    assert out.get("dry_run") is True
