"""最小 NodeExecutionContext 桩（独立模块，避免 conftest 同名导入歧义）。"""
from uuid import uuid4


class FakeExecution:
    express_prefix = "$"
    context: dict = {}
    # 每 pytest 进程唯一（E2E 数据目录以它派生；跨进程不复用，规避
    # colima/virtiofs 对删后重建同名路径的陈旧缓存）
    execution_id = "fake-exec-" + uuid4().hex[:8]

    def __init__(self, global_vars=None):
        self._globals = global_vars or {}

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return self._globals.get(key, default)
