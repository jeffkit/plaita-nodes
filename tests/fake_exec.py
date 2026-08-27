"""最小 NodeExecutionContext 桩（独立模块，避免 conftest 同名导入歧义）。"""


class FakeExecution:
    express_prefix = "$"
    context: dict = {}

    def __init__(self, global_vars=None):
        self._globals = global_vars or {}

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return self._globals.get(key, default)
