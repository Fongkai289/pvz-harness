from types import CodeType

import pytest

from rsi.code_runtime import (
    CandidateOutputError,
    CandidateProgram,
    CandidateValidationError,
    MAX_MEMORY_BYTES,
    MAX_SOURCE_BYTES,
    validate_source,
)


VALID = '''
def perceive(image):
    return {"value": image[0]}

def decide(observation, memory):
    memory["turn"] = memory.get("turn", 0) + 1
    if observation["value"] > 0:
        return {"kind": "click", "turn": memory["turn"]}
    return None
'''


def program_with(body, *, perception="return {}", helpers=None):
    source = "def perceive(image):\n    " + perception + "\n\ndef decide(observation, memory):\n"
    source += "\n".join("    " + line for line in body.splitlines()) + "\n"
    return CandidateProgram(source, helpers=helpers)


def test_compiles_and_preserves_explicit_episode_memory():
    assert isinstance(validate_source(VALID), CodeType)
    candidate = CandidateProgram(VALID)
    memory = {}
    observation = candidate.perceive([8])
    assert candidate.decide(observation, memory) == {"kind": "click", "turn": 1}
    assert candidate.decide(observation, memory)["turn"] == 2
    assert candidate.decide(candidate.perceive([0]), memory) is None
    assert memory == {"turn": 3}
    assert candidate.decide(observation, {})["turn"] == 1


def test_helpers_pure_functions_annotations_and_data_methods():
    source = '''
def positive(value: int, offset: int = 1) -> int:
    return max(0, value + offset)

def perceive(image) -> dict:
    width, height = image_size(image)
    values = [positive(pixel(image, x, 0)[0]) for x in range(width)]
    values.sort()
    return {"values": values, "height": height,
            "count": count_color(image, 0, 0, width, height, [1, 2, 3], tolerance=2),
            "components": components(image, [1, 2, 3], tolerance=0)}

def decide(observation: dict, memory: dict) -> dict | None:
    history = memory.setdefault("history", [])
    history.append(sum(value for value in observation["values"]))
    copy = history.copy()
    copy.extend([4, 5])
    copy.pop()
    pairs = {key: value for key, value in observation.items() if key == "height"}
    for i, value in enumerate(copy):
        if i == 0:
            continue
        pairs["extra"] = value
        break
    return {"pairs": pairs, "count": copy.count(4), "keys": list(memory.keys())}
'''
    image = object()
    seen = []

    def pixel(frame, x, y):
        assert frame is image
        seen.append((x, y))
        return [x, 0, 0]

    helpers = {
        "pixel": pixel,
        "image_size": lambda frame: [3, 2],
        "count_color": lambda *args, tolerance=0: tolerance,
        "components": lambda *args, tolerance=0: [{"x": 1, "y": 0}],
    }
    candidate = CandidateProgram(source, helpers=helpers)
    helpers.clear()  # construction snapshots the registry
    obs = candidate.perceive(image)
    assert obs == {"values": [1, 2, 3], "height": 2, "count": 2,
                   "components": [{"x": 1, "y": 0}]}
    memory = {}
    assert candidate.decide(obs, memory) == {
        "pairs": {"height": 2, "extra": 4}, "count": 1, "keys": ["history"]}
    assert memory == {"history": [6]}
    assert seen == [(0, 0), (1, 0), (2, 0)]


@pytest.mark.parametrize("body", [
    "import os\nreturn {}",
    "from pathlib import Path\nreturn {}",
    "return open('secret.txt').read()",
    "return eval('1 + 1')",
    "exec('pass')\nreturn {}",
    "return compile('pass', '', 'exec')",
    "return __import__('socket')",
    "return globals()",
    "return locals()",
    "return vars(observation)",
    "return dir(observation)",
    "return getattr(observation, '__class__')",
    "return observation.__class__",
    "return decide.__globals__",
    "return observation.get.__self__",
    "return observation.get('__class__').__mro__",
    "return observation.clear.__call__()",
    "return '{0.__class__}'.format(observation)",
    "return '{x}'.format_map(observation)",
    "return image_size.__code__",
    "return socket.connect(('localhost', 80))",
    "return judge.score(observation)",
    "return metrics",
    "return request('https://example.com')",
    "return observation['callback']()",
    "return observation.get('callback')()",
    "callback = len\nreturn callback(observation)",
    "return (lambda: {})()",
    "class Escape:\n    pass\nreturn {}",
    "def nested():\n    return {}\nreturn nested()",
    "global secret\nreturn {}",
    "nonlocal secret\nreturn {}",
    "while True:\n    pass",
    "with observation:\n    pass",
    "try:\n    return {}\nexcept:\n    return {}",
    "raise observation",
    "yield {}",
    "return f'{observation}'",
    "_hidden = 1\nreturn {}",
    "len = 3\nreturn {}",
    "perceive = 3\nreturn {}",
    "return len(**observation)",
    "return len(*observation)",
    "observation.get = 1\nreturn {}",
    "return {'bad': b'bytes'}",
    "return {'bad': 1j}",
    "return {'bad': 1e999}",
])
def test_rejects_escape_syntax_and_unauthorized_access(body):
    with pytest.raises(CandidateValidationError):
        program_with(body)


@pytest.mark.parametrize("source", [
    "def perceive(image):\n    return {}",
    VALID + "\nopen('secret.txt')",
    VALID + "\nstate = {}",
    VALID + "\n@decide\ndef decorated():\n    return {}",
    VALID + "\ndef extra(value=open('secret.txt')):\n    return value",
    VALID + "\ndef extra(value=[]):\n    return value",
    VALID + "\ndef extra(value: open('secret.txt')):\n    return value",
    VALID + "\ndef extra(value: dict.__class__):\n    return value",
    VALID + "\ndef extra(len):\n    return len",
    VALID + "\ndef extra(*args):\n    return args",
    VALID + "\ndef extra(**kwargs):\n    return kwargs",
    VALID + "\ndef extra(*, value=1):\n    return value",
    VALID + "\ndef extra(a, a):\n    return a",
    VALID + "\ndef extra():\n    break",
    VALID + "\ndef perceive(image):\n    return {}",
    VALID.replace("perceive(image)", "perceive(image, other)"),
    VALID.replace("decide(observation, memory)", "decide(observation, memory={})"),
    VALID.replace("def perceive", "async def perceive"),
    "not Python !",
    "\x00",
])
def test_rejects_invalid_module_contract(source):
    with pytest.raises(CandidateValidationError):
        validate_source(source)
    with pytest.raises(CandidateValidationError):
        CandidateProgram(source)


def test_source_and_ast_limits():
    with pytest.raises(CandidateValidationError, match="byte limit"):
        validate_source(VALID + "#" + "a" * MAX_SOURCE_BYTES)
    with pytest.raises(CandidateValidationError, match="byte limit"):
        validate_source(VALID + "#" + "界" * (MAX_SOURCE_BYTES // 2))
    large_ast = "def perceive(image):\n    return [" + "0," * 10_001 + "]\n" + VALID.split("def decide")[0]
    with pytest.raises(CandidateValidationError, match="AST node limit"):
        validate_source(large_ast)
    with pytest.raises(CandidateValidationError):
        validate_source(None)


@pytest.mark.parametrize("name", ["_pixel", "__import__", "open", "len", "perceive", "decide", "not valid", "class"])
def test_rejects_reserved_helper_names(name):
    with pytest.raises(CandidateValidationError):
        CandidateProgram(VALID, {name: lambda: None})


def test_helpers_must_be_explicit_and_callable():
    source = VALID.replace("image[0]", "pixel(image, 0, 0)")
    with pytest.raises(CandidateValidationError, match="unknown callable"):
        validate_source(source)
    assert isinstance(validate_source(source, ["pixel"]), CodeType)
    with pytest.raises(CandidateValidationError, match="callable"):
        CandidateProgram(source, {"pixel": 42})
    with pytest.raises(CandidateValidationError, match="rebind"):
        program_with("pixel = len\nreturn {}", helpers={"pixel": lambda: None})


def test_method_receiver_is_checked_before_dispatch():
    class Privileged:
        called = False

        def get(self, name):
            self.called = True
            return {}

    image = Privileged()
    candidate = program_with("return None", perception="return image.get('secret')")
    with pytest.raises(CandidateValidationError, match="plain data"):
        candidate.perceive(image)
    assert not image.called

    class PrivilegedDict(dict):
        def get(self, name):
            raise AssertionError("must never dispatch subclass methods")

    with pytest.raises(CandidateValidationError):
        candidate.perceive(PrivilegedDict())


@pytest.mark.parametrize("expression", [
    "{'bad': float('nan')}", "{'bad': float('inf')}",
    "{'bad': float('-inf')}", "{'bad': (1, 2)}", "{1: 'bad'}",
    "{'bad': decide}", "{'bad': 2 ** 5000}", "{'bad': '\\ud800'}",
    "{'bad': 'x' * 70000}", "{'bad': [0] * 10001}",
])
def test_rejects_non_json_or_unbounded_results(expression):
    candidate = program_with("return " + expression)
    with pytest.raises(CandidateOutputError):
        candidate.decide({}, {})
    candidate = program_with("return None", perception="return " + expression)
    with pytest.raises(CandidateOutputError):
        candidate.perceive(None)


@pytest.mark.parametrize("expression", ["[]", "42", "'text'", "(1,)"])
def test_top_level_outputs_are_dicts(expression):
    with pytest.raises(CandidateOutputError):
        program_with("return " + expression).decide({}, {})
    with pytest.raises(CandidateOutputError):
        program_with("return None", perception="return " + expression).perceive(None)
    with pytest.raises(CandidateOutputError):
        program_with("return None", perception="return None").perceive(None)


def test_memory_byte_limit_is_exact_and_counts_utf8():
    candidate = program_with("return None")
    candidate.decide({}, {"x": "a" * (MAX_MEMORY_BYTES - 8)})
    with pytest.raises(CandidateOutputError, match="memory exceeds"):
        candidate.decide({}, {"x": "a" * (MAX_MEMORY_BYTES - 7)})
    with pytest.raises(CandidateOutputError, match="memory exceeds"):
        candidate.decide({}, {"x": "界" * (MAX_MEMORY_BYTES // 2)})


@pytest.mark.parametrize("body", [
    "memory['cycle'] = memory\nreturn None",
    "memory['bad'] = float('nan')\nreturn None",
    "memory['big'] = 'x' * 70000\nreturn None",
    "memory['callback'] = decide\nreturn None",
    "memory['bad'] = float('inf')\nreturn 1 / 0",
])
def test_checks_memory_after_mutation_even_when_candidate_raises(body):
    with pytest.raises(CandidateOutputError, match="memory"):
        program_with(body).decide({}, {})


def test_json_cycle_depth_and_shared_references():
    candidate = program_with("return observation")
    cycle = {}
    cycle["self"] = cycle
    with pytest.raises(CandidateOutputError, match="cycle"):
        candidate.decide(cycle, {})
    deep = {}
    for i in range(40):
        deep = {"child": deep}
    with pytest.raises(CandidateOutputError, match="complexity"):
        candidate.decide({}, deep)
    shared = [1, 2]
    assert candidate.decide({"a": shared, "b": shared}, {}) == {"a": [1, 2], "b": [1, 2]}


@pytest.mark.parametrize("observation,memory", [([], {}), ({}, []), ({"bad": object()}, {}), ({}, {1: 2})])
def test_checks_inputs_before_candidate_execution(observation, memory):
    candidate = program_with("return 1 / 0")
    with pytest.raises(CandidateOutputError):
        candidate.decide(observation, memory)


def test_runtime_errors_propagate():
    with pytest.raises(ZeroDivisionError):
        program_with("return 1 / 0").decide({}, {})


def test_bounded_ranges_and_growing_list_loops(monkeypatch):
    import rsi.code_runtime as runtime

    monkeypatch.setattr(runtime, "MAX_ITERATIONS", 5)
    for body in (
        "return {'x': list(range(6))}",
        "return {'x': list(range(10 ** 100))}",
        "values = [0]\nfor value in values:\n    values.append(0)\nreturn {}",
        "return {'x': [x for x in [0] * 6]}",
        "return {'x': sum(x for x in [0] * 6)}",
    ):
        with pytest.raises(ValueError, match="iteration limit"):
            program_with(body).decide({}, {})
    assert program_with("return {'x': list(range(5))}").decide({}, {}) == {"x": [0, 1, 2, 3, 4]}
