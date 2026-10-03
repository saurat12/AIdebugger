"""Restricted AST worker. Launched with -I -S; never evals/imports target code."""

import ast
import builtins as python_builtins
import json
import math
import operator
import random
import re
import statistics
import sys
import time


class Unsupported(Exception):
    pass


class ExecutionLimit(Unsupported):
    """A verifier budget was exhausted, rather than a project defect proved."""


MAX_STEPS = 50_000
MAX_ITERATIONS = 1_000
MAX_OBJECTS = 1_000
MAX_DEPENDENCY_DEPTH = 16
MAX_LOCAL_BINDINGS = 64
SAFE_VALUE_TYPES = (bool, int, float, str, list, tuple, dict, set, range, type(None))


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class BreakLoop(Exception):
    pass


class ContinueLoop(Exception):
    pass


def bounded(value, depth=0, budget=None):
    budget = [0] if budget is None else budget
    budget[0] += len(value) if type(value) is str else 1
    if budget[0] > 64000:
        raise Unsupported("total value size limit")
    if depth > 12:
        raise Unsupported("value nesting limit")
    if type(value) is ReadOnlyRecord:
        bounded(value.fields, depth + 1, budget)
        return value
    if type(value) is Instance:
        fields = {name: item for name, item in value.cls.fields.items() if not isinstance(item, Function)}
        fields.update(value.fields)
        bounded(fields, depth + 1, budget)
        return value
    if type(value) is type and value in SAFE_VALUE_TYPES:
        return value
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 10**12:
            raise Unsupported("numeric limit")
        return value
    if type(value) is str:
        if len(value) > 10000:
            raise Unsupported("string limit")
        return value
    if type(value) in (list, tuple, dict, range, set):
        if len(value) > 1000:
            raise Unsupported("collection limit")
        if type(value) is dict:
            for key, item in value.items():
                bounded(key, depth + 1, budget)
                bounded(item, depth + 1, budget)
        else:
            for item in value:
                bounded(item, depth + 1, budget)
        return value
    raise Unsupported("non-JSON observation")


def json_safe(value):
    if type(value) is type and value in SAFE_VALUE_TYPES:
        return {"builtin_type": value.__name__}
    if type(value) is ReadOnlyRecord:
        return json_safe(value.fields)
    if type(value) is Instance:
        fields = {name: item for name, item in value.cls.fields.items() if not isinstance(item, Function)}
        fields.update(value.fields)
        return json_safe(fields)
    if type(value) is set:
        return [json_safe(item) for item in sorted(value, key=repr)]
    if type(value) is range:
        return list(value)
    if type(value) in (list, tuple):
        return [json_safe(item) for item in value]
    if type(value) is dict:
        return {key: json_safe(item) for key, item in value.items()}
    return value


class Function:
    def __init__(self, node, defaults, owner=None, kwdefaults=None, closure=None):
        self.node, self.defaults, self.owner = node, defaults, owner
        self.kwdefaults = kwdefaults or {}
        self.closure = closure or {}


class ConstructorAdapter:
    """Trusted, bounded data adapter; the plan may select but never define one."""

    def __init__(self, adapter_id, input_shape, maximum_size, binding, read_only_operations,
                 allow_nested, deterministic, construct, dependency=None):
        self.adapter_id, self.input_shape, self.maximum_size = adapter_id, input_shape, maximum_size
        self.binding, self.read_only_operations = binding, frozenset(read_only_operations)
        self.allow_nested, self.deterministic = allow_nested, deterministic
        self.construct, self.dependency = construct, dependency


class BoundedGenerator:
    """Lazy internal iterator; no generator attributes are exposed to source."""

    def __init__(self, iterator):
        self.iterator = iterator

    def __iter__(self):
        return self.iterator


class Class:
    def __init__(self):
        self.fields = {}


class Instance:
    def __init__(self, cls):
        self.cls, self.fields = cls, {}


class ReadOnlyRecord:
    """Verifier-owned data only; target code cannot access host descriptors."""

    def __init__(self, fields):
        if any(not isinstance(name, str) or not name.isidentifier() or name.startswith("__") for name in fields):
            raise Unsupported("record fields must be local identifiers without dunder access")
        self.fields = dict(fields)
        bounded(self.fields)


def trusted_constructor_adapters():
    """Trusted-code registry shared by planning validation and the worker."""
    return {
        "list": ConstructorAdapter("list", list, 1000, "SAFE_LITERAL", {"snapshot", "size", "index"}, True, True, list),
        "tuple": ConstructorAdapter("tuple", list, 1000, "SAFE_LITERAL", {"snapshot", "size", "index"}, True, True, tuple),
        "dict": ConstructorAdapter("dict", dict, 1000, "SAFE_LITERAL", {"snapshot", "size", "key"}, True, True, dict),
        "set": ConstructorAdapter("set", list, 1000, "SAFE_LITERAL", {"snapshot", "size", "contains"}, False, True, set),
        "record": ConstructorAdapter("record", dict, 1000, "SAFE_DERIVED_BINDING", {"snapshot", "field"}, True, True, ReadOnlyRecord),
    }


class File:
    def __init__(self, content, mode):
        self.content, self.closed, self.mode = ("" if mode == "w" else content), False, mode
        self.position = len(content) if mode == "a" else 0


class SafeCall:
    def __init__(self, function, arity=None, keywords=False, safe_tag=None):
        self.function = function
        self.arity = arity
        self.keywords = keywords
        self.safe_tag = safe_tag


class BuiltinCapabilityRegistry:
    """Trusted pure builtins and their bounded call/observation contracts."""

    def __init__(self):
        self.entries = {}

    def register(self, name, function, *, arity=None, keywords=False, observable=True, binding=None):
        if name in self.entries or not callable(function):
            raise Unsupported("invalid safe builtin registration")
        self.entries[name] = {"binding": binding if binding is not None else SafeCall(function, arity, keywords, "pure_builtin"),
                              "arity": arity, "keywords": keywords, "observable": observable}

    def resolve(self, name):
        return self.entries.get(name, {}).get("binding")


class Stream:
    def __init__(self, name):
        self.name = name


class ModuleBinding:
    """Opaque adapter reference: expose only explicitly requested safe members."""

    def __init__(self, name, module, exports, category):
        self.name, self.module, self.exports, self.category = name, module, exports, category
        self.selected = {}


class DependencyResolver:
    """Trusted symbol adapters and lazy AST bindings; never import project code."""

    CATEGORIES = {"LOCAL_SAFE", "SAFE_STDLIB", "SAFE_INSTALLED_LIBRARY", "SAFE_LITERAL",
                  "SAFE_DERIVED_BINDING", "UNSAFE_SIDE_EFFECT", "UNSAFE", "UNRESOLVED"}

    def __init__(self, engine):
        self.engine = engine
        self.modules = {}
        self.classifications = {}

    def register_module(self, name, symbols, category="SAFE_STDLIB"):
        # Registrations are trusted application code, never DSL input. Optional
        # installed-library adapters must already be loaded by the trusted host.
        if category not in {"SAFE_STDLIB", "SAFE_INSTALLED_LIBRARY"} or name not in sys.modules:
            raise Unsupported("safe dependency adapter is unavailable: " + name)
        self.modules[name] = (symbols, category)

    def imported(self, name, module, member):
        if module not in self.modules:
            self.classifications[name] = "UNSAFE_SIDE_EFFECT" if module in sys.modules else "UNRESOLVED"
            dependency = module + ("." + member if member else "")
            raise Unsupported("required import is not allowlisted: " + dependency)
        symbols, category = self.modules[module]
        if member is None:
            self.engine.allocate_object()
            self.classifications[name] = category
            return ModuleBinding(name, module, symbols, category)
        return self.member(name, module, symbols, category, member)

    def member(self, name, module, symbols, category, member):
        if member.startswith("__"):
            raise Unsupported("dunder/reflection dependency access is blocked: " + member)
        if type(symbols) is dict:
            if member not in symbols:
                self.classifications[name] = "UNRESOLVED"
                raise Unsupported(f"standard-library member is not allowlisted: {module}.{member}")
            value = symbols[member]
        else:
            # Only a trusted opaque adapter (e.g. bounded randomness) can
            # provide operations here; the interpreter never uses host getattr.
            value = self.engine.attribute(symbols, member)
        if isinstance(value, SafeCall):
            if value.safe_tag not in {"pure_dependency", "pure_builtin", "read_only", "deterministic_random"}:
                self.classifications[name] = "UNSAFE"
                raise Unsupported("unsafe dependency binding: " + module + "." + member)
        elif not isinstance(value, Stream):
            bounded(value)
        self.classifications[name] = category
        return value

    def is_module(self, value):
        return isinstance(value, ModuleBinding) or any(value is symbols for symbols, _ in self.modules.values())

    def resolve(self, name):
        self.engine.tick()
        return self.engine._resolve_dependency(name)


ERRORS = {kind.__name__: kind for kind in (IndexError, ZeroDivisionError, ValueError, TypeError, KeyError, RuntimeError, AssertionError, NameError)}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


class Engine:
    def __init__(self, source, spec):
        self.spec = spec
        self.source = source
        self.globals = {}
        self.files = []
        self.draws = []
        self.depth = 0
        self.instances_created = 0
        self.steps = 0
        self.iterations = 0
        self.callback_context = 0
        self.stdout = []
        self.stderr = []
        self.stdout_stream = Stream("stdout")
        self.stderr_stream = Stream("stderr")
        self.rng = random.Random(spec.get("seed", 0))
        self.stubs = {key: list(values) for key, values in spec.get("random_values", {}).items()}
        self.boundaries = {key: list(values) for key, values in spec.get("random_boundaries", {}).items()}
        self.virtual_time = spec.get("clock", {}).get("start")
        self.time_advanced = 0
        self.random_module = object()
        self.builtin_policy = BuiltinCapabilityRegistry()
        for name, function in {
            "len": len, "abs": abs, "list": list, "tuple": tuple, "dict": dict,
            "set": set, "range": range, "int": int, "float": float, "str": str, "bool": bool,
            "all": lambda values: all(self.iterable(values)),
            "any": lambda values: any(self.iterable(values)),
            "sum": self.safe_sum, "min": lambda *a, **kw: self.safe_extremum(min, *a, **kw),
            "max": lambda *a, **kw: self.safe_extremum(max, *a, **kw),
            "sorted": self.safe_sorted, "enumerate": self.safe_enumerate,
            "zip": self.safe_zip, "round": round,
            "type": self.safe_type, "isinstance": self.safe_isinstance,
        }.items():
            keywords = {"dict": True, "round": ("number", "ndigits"), "sum": ("start",),
                        "min": ("key", "default"), "max": ("key", "default"),
                        "sorted": ("key", "reverse"), "enumerate": ("start",),
                        "zip": ("strict",)}.get(name, False)
            arity = {"type": (1, 1), "isinstance": (2, 2)}.get(name)
            binding = function if function in SAFE_VALUE_TYPES else None
            self.builtin_policy.register(name, function, arity=arity, keywords=keywords, binding=binding)
        self.builtins = {name: entry["binding"] for name, entry in self.builtin_policy.entries.items()}
        # These are restricted virtual I/O operations, never pure builtin capabilities.
        self.builtins["print"] = SafeCall(self.print_values, keywords=True)
        self.builtins["open"] = SafeCall(self.open_file, (1, 2))
        tree = ast.parse(source)
        self.tree = tree
        if sum(1 for _ in ast.walk(tree)) > 5000:
            raise Unsupported("source node limit")
        self.definitions = {}
        self.binding_lines = {}
        self.loading = set()
        self.ambiguous = set()
        self.dependencies = DependencyResolver(self)
        self.constructor_adapters = trusted_constructor_adapters()
        self.adapter_bindings = {}
        self.local_stubs = spec.get("stubs", {})
        self.stub_calls = {name: 0 for name in self.local_stubs}
        self.dependency_classifications = self.dependencies.classifications
        self.safe_modules = {
            "math": {name: SafeCall(getattr(math, name), safe_tag="pure_dependency") for name in ("sqrt", "floor", "ceil", "fabs", "isfinite", "isnan")},
            "statistics": {name: SafeCall(getattr(statistics, name), safe_tag="pure_dependency") for name in ("mean", "median", "fmean", "variance", "stdev")},
        }
        self.safe_modules["statistics"]["linear_regression"] = SafeCall(self.linear_regression, keywords=("proportional",), safe_tag="pure_dependency")
        self.safe_modules["types"] = {"SimpleNamespace": SafeCall(self.make_record, keywords=True, safe_tag="pure_dependency")}
        self.safe_modules["math"].update(pi=math.pi, e=math.e)
        self.safe_modules["sys"] = {"stdout": self.stdout_stream, "stderr": self.stderr_stream}
        if self.virtual_time is not None:
            self.safe_modules["time"] = {"time": SafeCall(lambda: self.virtual_time, (0, 0), safe_tag="pure_dependency")}
        for name, symbols in self.safe_modules.items():
            self.dependencies.register_module(name, symbols)
        self.dependencies.register_module("random", self.random_module)
        for statement in tree.body:
            bindings = []
            if isinstance(statement, (ast.FunctionDef, ast.ClassDef)):
                bindings = [(statement.name, statement)]
            elif isinstance(statement, ast.Assign):
                bindings = [(target.id, statement.value) for target in statement.targets if isinstance(target, ast.Name)]
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
                bindings = [(statement.target.id, statement.value)]
            elif isinstance(statement, ast.Import):
                bindings = [(alias.asname or alias.name.split(".")[0], (alias.name, None)) for alias in statement.names]
            elif isinstance(statement, ast.ImportFrom):
                bindings = [(alias.asname or alias.name, (statement.module if not statement.level else "", alias.name)) for alias in statement.names]
            elif not self.doc_or_pass(statement):
                # Mark only names mutated by unrelated module statements. A
                # required binding with import-time mutation cannot be safely
                # reconstructed, while unrelated setup remains irrelevant.
                for node in ast.walk(statement):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        base = node.func.value
                        while isinstance(base, (ast.Attribute, ast.Subscript)):
                            base = base.value
                        if isinstance(base, ast.Name):
                            self.ambiguous.add(base.id)
            for name, definition in bindings:
                if name in self.definitions:
                    self.ambiguous.add(name)
                self.definitions[name] = definition
                self.binding_lines[name] = getattr(statement, "lineno", 0)
        self.stub_nodes = {name: self.definitions.get(name) for name in self.local_stubs}

    def register_constructor_adapter(self, name, adapter):
        """Trusted host registration only; JSON plans cannot install adapters."""
        if (not isinstance(name, str) or not name.isidentifier() or name.startswith("__") or
                not isinstance(adapter, ConstructorAdapter) or adapter.adapter_id != name or
                adapter.input_shape not in (list, dict) or
                type(adapter.maximum_size) is not int or not 1 <= adapter.maximum_size <= 1000 or
                adapter.binding not in {"SAFE_LITERAL", "SAFE_DERIVED_BINDING", "SAFE_STDLIB", "SAFE_INSTALLED_LIBRARY"} or
                not adapter.read_only_operations or not adapter.read_only_operations <= {"snapshot", "size", "key", "index", "field", "contains"} or
                type(adapter.allow_nested) is not bool or adapter.deterministic is not True or not callable(adapter.construct)):
            raise Unsupported("constructor adapter contract is invalid")
        if adapter.binding == "SAFE_INSTALLED_LIBRARY" and (not adapter.dependency or adapter.dependency not in sys.modules):
            raise Unsupported("constructor dependency is not installed and allowlisted: " + str(adapter.dependency))
        self.constructor_adapters[name] = adapter

    def resolve(self, name):
        return self.dependencies.resolve(name)

    def tick(self, amount=1, *, iterations=0):
        self.steps += amount
        self.iterations += iterations
        if self.steps > MAX_STEPS:
            raise ExecutionLimit("execution limit exceeded: step budget")
        if self.iterations > MAX_ITERATIONS:
            raise ExecutionLimit("execution limit exceeded: loop/comprehension iteration budget")

    def allocate_object(self):
        self.instances_created += 1
        if self.instances_created > MAX_OBJECTS:
            raise ExecutionLimit("execution limit exceeded: object allocation budget")

    def _resolve_dependency(self, name):
        if name in self.ambiguous:
            self.dependency_classifications[name] = "UNSAFE_SIDE_EFFECT"
            raise Unsupported("required dependency has ambiguous or side-effectful initialization")
        if name in self.globals:
            return self.globals[name]
        if name in self.loading:
            raise Unsupported("cyclic initialization dependency")
        if name not in self.definitions:
            if name in self.builtins:
                return self.builtins[name]
            if hasattr(python_builtins, name):
                raise Unsupported("unsupported safe builtin: " + name)
            self.dependency_classifications[name] = "UNRESOLVED"
            if self.loading:
                raise Unsupported(f"unresolved local dependency: {name}; searched selected source file; dependency slice found no candidate")
            raise NameError(f"name '{name}' is not defined")
        if len(self.loading) >= MAX_DEPENDENCY_DEPTH:
            raise ExecutionLimit("execution limit exceeded: dependency depth")
        if len(self.globals) >= MAX_LOCAL_BINDINGS:
            raise ExecutionLimit("execution limit exceeded: local binding count")
        self.loading.add(name)
        try:
            definition = self.definitions[name]
            if isinstance(definition, (ast.FunctionDef, ast.ClassDef)):
                self.dependency_classifications[name] = "LOCAL_SAFE"
            elif isinstance(definition, tuple):
                self.dependency_classifications[name] = "UNRESOLVED"
            elif isinstance(definition, ast.Constant) or (isinstance(definition, (ast.List, ast.Tuple, ast.Dict)) and
                    all(isinstance(child, (ast.Constant, ast.Load, ast.List, ast.Tuple, ast.Dict)) for child in ast.walk(definition))):
                self.dependency_classifications[name] = "SAFE_LITERAL"
            else:
                self.dependency_classifications[name] = "SAFE_DERIVED_BINDING"
            if isinstance(definition, tuple):
                module, member = definition
                value = self.dependencies.imported(name, module, member)
            elif isinstance(definition, ast.FunctionDef):
                value = self.define(definition)
            elif isinstance(definition, ast.ClassDef):
                if definition.bases or definition.decorator_list or definition.keywords:
                    raise Unsupported("class inheritance/decorators")
                value = Class()
                self.allocate_object()
                for member in definition.body:
                    if isinstance(member, ast.FunctionDef):
                        if member.name.startswith("__") and member.name != "__init__":
                            raise Unsupported("custom object protocol")
                        value.fields[member.name] = self.define(member)
                    elif isinstance(member, ast.Assign):
                        content = self.safe_initializer(member.value, value.fields)
                        for target in member.targets:
                            if not isinstance(target, ast.Name):
                                raise Unsupported("class assignment")
                            value.fields[target.id] = content
                    elif not self.doc_or_pass(member):
                        raise Unsupported("class body")
            else:
                value = self.safe_initializer(definition, {})
            self.globals[name] = value
            return value
        except Unsupported:
            if self.dependency_classifications.get(name) not in {"UNSAFE", "UNSAFE_SIDE_EFFECT"}:
                self.dependency_classifications[name] = "UNRESOLVED"
            raise
        finally:
            self.loading.remove(name)

    def safe_initializer(self, node, env):
        permitted = (ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple, ast.Dict, ast.Set,
                     ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
                     ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor,
                     ast.LShift, ast.RShift, ast.USub, ast.UAdd, ast.Not, ast.Invert,
                     ast.Call, ast.Attribute, ast.keyword)
        if any(not isinstance(child, permitted) for child in ast.walk(node)):
            raise Unsupported("required initializer is not a safe constant expression")
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if any(keyword.arg is None for keyword in child.keywords):
                    raise Unsupported("required initializer cannot expand arguments")
                function = self.expr(child.func, env)
                if not isinstance(function, SafeCall) or function.safe_tag not in ("pure_dependency", "pure_builtin"):
                    raise Unsupported("required initializer call is not an allowlisted pure dependency")
        result = self.expr(node, env)
        if isinstance(result, Function) or (isinstance(result, SafeCall) and result.safe_tag in {"pure_dependency", "pure_builtin", "read_only"}):
            return result  # A safe alias binds a callable without executing its body.
        return bounded(result)

    def make_record(self, **fields):
        self.allocate_object()
        return ReadOnlyRecord(fields)

    def linear_regression(self, x, y, *, proportional=False):
        # Invoke a trusted stdlib symbol on bounded data and copy only its
        # documented data fields, never expose the native result object.
        if type(x) not in (list, tuple) or type(y) not in (list, tuple) or type(proportional) is not bool:
            raise Unsupported("statistics.linear_regression requires bounded sequences and a boolean option")
        bounded(x)
        bounded(y)
        self.tick(iterations=len(x))
        result = statistics.linear_regression(x, y, proportional=proportional)
        return self.make_record(slope=result.slope, intercept=result.intercept)

    @staticmethod
    def doc_or_pass(node):
        return isinstance(node, ast.Pass) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))

    def define(self, node):
        if node.decorator_list or node.args.vararg or node.args.kwarg:
            raise Unsupported("function signature/decorators")
        return Function(node, [self.safe_initializer(default, {}) for default in node.args.defaults],
                        kwdefaults={arg.arg: self.safe_initializer(default, {})
                                    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None})

    def open_file(self, name, mode="r"):
        if not isinstance(name, str) or name.startswith(("/", "\\")) or ":" in name or ".." in name.replace("\\", "/").split("/"):
            raise Unsupported("virtual resource path must be relative")
        if mode not in ("r", "w", "a") or name not in self.spec.get("files", {}):
            raise Unsupported("resource not explicitly supplied")
        if len(self.files) >= 100:
            raise Unsupported("resource limit")
        self.allocate_object()
        file = File(bounded(self.spec["files"][name]), mode)
        self.files.append(file)
        return file

    def print_values(self, *values, sep=" ", end="\n", file=None):
        if not isinstance(sep, str) or not isinstance(end, str):
            raise Unsupported("print formatting")
        if file is not None and file not in (self.stdout_stream, self.stderr_stream):
            raise Unsupported("print stream")
        stream = self.stderr if file is self.stderr_stream else self.stdout
        for value in values:
            bounded(value)
        bounded(sep); bounded(end)
        text = sep.join(str(value) for value in values) + end
        if sum(map(len, self.stdout + self.stderr)) + len(text) > 8000:
            raise Unsupported("captured stdout limit")
        stream.append(text)

    def run_plan(self, plan):
        instances, results, observed = {}, [], {}
        for operation in plan["steps"]:
            self.tick()
            op = operation["op"]
            if op == "construct":
                cls = self.resolve(operation["symbol"])
                value = self.invoke(cls, self.plan_value(operation.get("args", []), instances),
                                    self.plan_value(operation.get("kwargs", {}), instances))
                instances[operation["as"]] = value
                result = value
            elif op == "construct_value":
                adapter = self.constructor_adapters.get(operation["constructor"])
                if adapter is None:
                    raise Unsupported("constructor dependency is unavailable: " + operation["constructor"])
                raw = operation["value"]
                if type(raw) is not adapter.input_shape or len(raw) > adapter.maximum_size:
                    raise Unsupported("constructor input shape or size is unsupported")
                if not adapter.allow_nested and any(type(item) in (dict, list) for item in (raw.values() if type(raw) is dict else raw)):
                    raise Unsupported("constructor adapter does not allow nested inputs")
                self.allocate_object()
                value = adapter.construct(self.plan_value(raw, instances))
                instances[operation["as"]] = bounded(value)
                self.adapter_bindings[operation["as"]] = adapter
                result = value
            elif op == "bind_callable":
                value = self.resolve(operation["symbol"])
                if not isinstance(value, Function) and not (isinstance(value, SafeCall) and value.safe_tag in {"pure_builtin", "pure_dependency", "read_only"}):
                    raise Unsupported("bound callable is not a safe local or dependency binding")
                instances[operation["as"]] = value
                result = None
            elif op == "advance_time":
                if self.virtual_time is None:
                    raise Unsupported("deterministic time source cannot be substituted")
                self.time_advanced += operation["seconds"]
                if self.time_advanced > 86400:
                    raise ExecutionLimit("execution limit exceeded: virtual-time advancement")
                self.virtual_time = bounded(self.virtual_time + operation["seconds"])
                observed[operation["as"]] = self.virtual_time
                result = None
            elif op == "state_setup":
                state = self.local_state(operation["symbol"])
                action = operation["action"]
                if action == "clear" and type(state) in (dict, list, set):
                    state.clear()
                elif action == "insert" and type(state) is dict and type(operation["value"]) is dict and set(operation["value"]) == {"key", "value"}:
                    key = bounded(operation["value"]["key"])
                    if type(key) not in (str, int, float, bool):
                        raise Unsupported("module-state dictionary key must be a scalar")
                    state[key] = bounded(operation["value"]["value"])
                elif action == "insert" and type(state) in (list, set):
                    value = bounded(operation["value"])
                    if type(state) is set and type(value) not in (str, int, float, bool):
                        raise Unsupported("module-state set entry must be a scalar")
                    state.add(value) if type(state) is set else state.append(value)
                elif action == "reset" and type(state) in (str, int, float, bool, type(None)) and type(operation["value"]) is type(state):
                    self.globals[operation["symbol"]] = bounded(operation["value"])
                else:
                    raise Unsupported("module-state setup action is incompatible with selected local state")
                bounded(self.globals[operation["symbol"]])
                observed[operation["as"]] = None
                result = None
            elif op == "observe_state":
                state = self.local_state(operation["symbol"])
                mode = operation["mode"]
                if mode == "snapshot":
                    value = json_safe(bounded(state))
                elif mode == "size" and type(state) in (dict, list, set, tuple, str):
                    value = len(state)
                elif mode == "contains" and type(state) in (dict, list, set, tuple, str):
                    key = bounded(operation["key"])
                    if type(key) not in (str, int, float, bool):
                        raise Unsupported("module-state membership key must be scalar")
                    value = key in state
                else:
                    raise Unsupported("module state is not safely observable with selected mode")
                observed[operation["as"]] = value
                result = None
            elif op == "call":
                target = operation["target"]
                if "." in target:
                    owner, member = target.split(".", 1)
                    if owner not in instances or not member.isidentifier() or member.startswith("__"):
                        raise Unsupported("plan call target")
                    callable_value = self.attribute(instances[owner], member)
                else:
                    if not target.isidentifier() or target.startswith("__"):
                        raise Unsupported("plan call target")
                    callable_value = instances[target] if target in instances else self.resolve(target)
                call_args = self.plan_value(operation.get("args", []), instances)
                result = self.invoke(callable_value, call_args, self.plan_value(operation.get("kwargs", {}), instances))
                observed["args_after"] = bounded(call_args)
                bounded(result)
                results.append(result)
                if "as" in operation:
                    observed[operation["as"]] = json_safe(bounded(result))
                    instances[operation["as"]] = result
            elif op == "observe":
                target = operation["target"]
                parts = target.split(".")
                value = self.resolve(parts[0]) if parts[0] not in instances else instances[parts[0]]
                for position, member in enumerate(parts[1:]):
                    if member.startswith("__"):
                        raise Unsupported("dunder/reflection observation is blocked")
                    if position == 0 and parts[0] in self.adapter_bindings:
                        adapter = self.adapter_bindings[parts[0]]
                        access_kind = "key" if type(value) is dict else "index" if type(value) in (list, tuple) else "field"
                        if access_kind not in adapter.read_only_operations:
                            raise Unsupported("constructor adapter does not allow read-only " + access_kind + " observation")
                    try:
                        if type(value) is dict:
                            value = value[member]
                        elif type(value) in (list, tuple) and member.isdecimal():
                            value = value[int(member)]
                        else:
                            value = self.attribute(value, member)
                    except (KeyError, IndexError):
                        raise Unsupported("observation path does not exist in bounded result") from None
                    if isinstance(value, (Function, SafeCall, ModuleBinding)):
                        raise Unsupported("observation cannot expose callable or module attributes")
                observed[operation["as"]] = json_safe(bounded(value))
            else:
                raise Unsupported("unknown plan operation")
        observed["last_result"] = json_safe(bounded(results[-1])) if results else None
        observed["return"] = observed["last_result"]
        observed["stdout"] = "".join(self.stdout)
        observed["stderr"] = "".join(self.stderr)
        observed["args_after"] = observed.get("args_after", [])
        observed["resources_open"] = sum(not file.closed for file in self.files)
        observed["resources_created"] = len(self.files)
        observed["stub_calls"] = dict(self.stub_calls)
        if self.virtual_time is not None:
            observed["virtual_time"] = self.virtual_time
        return observed

    def local_state(self, name):
        definition = self.definitions.get(name)
        if name in self.ambiguous or not isinstance(definition, (ast.Constant, ast.Dict, ast.List, ast.Set)):
            raise Unsupported("module state is not safely observable: selected binding is not an unambiguous local literal")
        value = self.resolve(name)
        if type(value) not in (dict, list, set, tuple, str, int, float, bool, type(None)):
            raise Unsupported("module state is not a bounded local collection or scalar")
        return bounded(value)

    def plan_value(self, value, bindings, depth=0):
        if depth > 8:
            raise ExecutionLimit("execution limit exceeded: plan value depth")
        if type(value) is dict:
            if set(value) == {"$ref"}:
                name = value["$ref"]
                if name not in bindings:
                    raise Unsupported("plan reference is not bound: " + str(name)[:80])
                return bindings[name]
            return bounded({key: self.plan_value(item, bindings, depth + 1) for key, item in value.items()})
        if type(value) is list:
            return bounded([self.plan_value(item, bindings, depth + 1) for item in value])
        return bounded(value)

    def run_module_fragment(self, plan):
        self.module_mode = True
        start, end = plan["target_lines"]
        selected = [node for node in self.tree.body
                    if getattr(node, "lineno", 0) <= end and getattr(node, "end_lineno", getattr(node, "lineno", 0)) >= start]
        if not selected:
            raise Unsupported("module fragment target lines do not select a top-level statement")
        for name in plan.get("required_bindings", []):
            if self.binding_lines.get(name, 0) >= start:
                raise Unsupported("module fragment binding is not defined before target lines: " + name)
            self.resolve(name)
        env = {}
        self.module_env = env
        last = None
        if plan.get("target_kind", "statement") == "expression":
            candidates = [node for statement in selected for node in ast.walk(statement)
                          if isinstance(node, ast.expr) and getattr(node, "lineno", 0) <= end
                          and getattr(node, "end_lineno", getattr(node, "lineno", 0)) >= start
                          and not (isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load))]
            if not candidates:
                raise Unsupported("module fragment target lines contain no expression")
            target = min(candidates, key=lambda node: (len(ast.get_source_segment(self.source, node) or ""),
                                                       getattr(node, "col_offset", 0)))
            last = self.expr(target, env)
            self.module_result = last
            return {"result": bounded(last), "state": {}, "stdout": "", "stderr": "",
                    "dependencies": dict(self.dependency_classifications)}
        for statement in selected:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                raise Unsupported("module fragment requires unsafe definition or import execution")
            if isinstance(statement, ast.Expr):
                last = self.expr(statement.value, env)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = self.expr(statement.value, env) if statement.value else None
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    self.assign(target, value, env)
                last = value
            else:
                self.block([statement], env)
            self.module_result = last
        bounded(env)
        return {"result": bounded(last), "state": bounded(env), "stdout": "".join(self.stdout), "stderr": "".join(self.stderr),
                "dependencies": dict(self.dependency_classifications)}

    def draw(self, name, *args):
        if len(self.draws) >= 100:
            raise Unsupported("random draw limit")
        if name == "randint":
            lower, upper = args
            allowed = lambda value: type(value) is int and lower <= value <= upper
        elif name == "randrange":
            choices = bounded(range(*args))
            allowed = lambda value: type(value) is int and value in choices
        elif name == "random":
            allowed = lambda value: type(value) in (int, float) and 0 <= value < 1
        else:
            raise Unsupported("random operation")
        if self.boundaries.get(name):
            mode = self.boundaries[name].pop(0)
            if name == "randint":
                value = lower if mode == "lower" else upper
            elif name == "randrange":
                if not choices:
                    raise ValueError("empty range")
                value = choices[0] if mode == "lower" else choices[-1]
            else:
                value = 0.0 if mode == "lower" else 0.9999999999999999
            if not allowed(value):
                raise Unsupported("random boundary outside valid range")
        elif self.stubs.get(name):
            value = self.stubs[name].pop(0)
            if not allowed(value):
                raise Unsupported("random stub is outside the API's valid range")
        else:
            value = getattr(self.rng, name)(*args)
        self.draws.append({"operation": name, "args": list(args), "value": value})
        return value

    def attribute(self, value, name):
        self.tick()
        if name.startswith("__"):
            raise Unsupported("dunder/reflection attribute is blocked: " + name)
        if isinstance(value, ModuleBinding):
            if name not in value.selected:
                value.selected[name] = self.dependencies.member(value.name + "." + name, value.module,
                                                                value.exports, value.category, name)
            return value.selected[name]
        if type(value) is ReadOnlyRecord:
            if name not in value.fields:
                raise Unsupported("verifier-owned record has no data field: " + name)
            return bounded(value.fields[name])
        for module, _ in self.dependencies.modules.values():
            if value is module and type(module) is dict:
                if name not in module:
                    raise Unsupported("standard-library member is not allowlisted: " + name)
                return module[name]
        if value is self.random_module:
            if name not in ("randint", "randrange", "random"):
                raise Unsupported("random operation")
            arity = {"randint": (2, 2), "randrange": (1, 3), "random": (0, 0)}[name]
            return SafeCall(lambda *args: self.draw(name, *args), arity, safe_tag="deterministic_random")
        if isinstance(value, (Instance, Class)):
            fields = value.fields
            if isinstance(value, Instance):
                result = fields[name] if name in fields else value.cls.fields.get(name)
            else:
                result = fields.get(name)
            if result is None and name not in fields and not (isinstance(value, Instance) and name in value.cls.fields):
                raise Unsupported("unknown state attribute")
            if isinstance(result, Function) and isinstance(value, Instance):
                return Function(result.node, result.defaults, value, result.kwdefaults)
            return result
        if isinstance(value, File):
            if name == "closed":
                return value.closed
            if name == "close":
                return SafeCall(lambda: setattr(value, "closed", True), (0, 0))
            if name == "read":
                def read(size=-1):
                    if value.closed:
                        raise ValueError("I/O operation on closed file")
                    if value.mode != "r":
                        raise Unsupported("reading non-readable virtual file")
                    if type(size) is not int:
                        raise Unsupported("read size")
                    end = len(value.content) if size < 0 else min(len(value.content), value.position + size)
                    text = value.content[value.position:end]
                    value.position = end
                    return text
                return SafeCall(read, (0, 1))
            if name == "write":
                def write(text):
                    if value.closed:
                        raise ValueError("I/O operation on closed file")
                    if not isinstance(text, str):
                        raise TypeError("Text required")
                    if value.mode not in ("w", "a"):
                        raise Unsupported("writing non-writable virtual file")
                    position = len(value.content) if value.mode == "a" else value.position
                    value.content = bounded(value.content[:position] + text + value.content[position + len(text):])
                    value.position = position + len(text)
                    return len(text)
                return SafeCall(write, (1, 1))
        permitted = {list: {"append", "extend", "remove", "pop", "clear", "copy"}, dict: {"get", "pop", "keys", "values", "items", "copy"}, str: {"lower", "upper", "strip", "split"}}
        if name in permitted.get(type(value), set()):
            def method(*args):
                if type(value) is list and name in {"append", "extend"}:
                    added = 1 if name == "append" else len(self.iterable(args[0])) if args else 0
                    if len(value) + added > 1000:
                        raise Unsupported("collection growth limit")
                result = getattr(value, name)(*args)
                bounded(value)
                return list(result) if name in ("keys", "values", "items") else result
            return SafeCall(method, safe_tag="read_only" if name in {"get", "keys", "values", "items", "copy", "lower", "upper", "strip", "split"} else None)
        raise Unsupported(f"read-only attribute '{name}' is unsupported for {type(value).__name__}; native descriptors and reflection are blocked")

    def iterable(self, values, *, charge=True):
        if type(values) is BoundedGenerator:
            return iter(values)
        if type(values) not in (list, tuple, dict, set, range, str):
            raise Unsupported("iteration requires a bounded local collection")
        bounded(values)
        if charge:
            self.tick(iterations=len(values))
        return values

    def key_function(self, key):
        if key is None:
            return None
        if not isinstance(key, Function) and not (isinstance(key, SafeCall) and key.safe_tag in {"pure_builtin", "pure_dependency", "read_only"}):
            raise Unsupported("key callable must be a safe local function or explicitly safe binding")
        def call(value):
            self.callback_context += 1
            try:
                return bounded(self.invoke(key, [value]))
            finally:
                self.callback_context -= 1
        return call

    def safe_extremum(self, operation, *args, **kwargs):
        key = self.key_function(kwargs.pop("key", None))
        if len(args) == 1:
            return operation(self.iterable(args[0]), key=key, **kwargs)
        if "default" in kwargs:
            raise TypeError("default is only supported for one iterable")
        self.tick(iterations=len(args))
        return operation(*args, key=key)

    def safe_sorted(self, values, *, key=None, reverse=False):
        if type(reverse) is not bool:
            raise Unsupported("sorted reverse must be a boolean")
        return sorted(self.iterable(values), key=self.key_function(key), reverse=reverse)

    def safe_sum(self, values, start=0):
        return sum(self.iterable(values), bounded(start))

    def safe_type(self, value):
        kind = type(value)
        if kind not in SAFE_VALUE_TYPES:
            raise Unsupported("safe builtin type cannot observe an external or dynamic object")
        return kind

    def safe_isinstance(self, value, kinds):
        allowed = kinds if type(kinds) is tuple else (kinds,)
        if not allowed or any(type(kind) is not type or kind not in SAFE_VALUE_TYPES for kind in allowed):
            raise Unsupported("safe builtin isinstance requires allowlisted type bindings")
        if type(value) not in SAFE_VALUE_TYPES:
            raise Unsupported("safe builtin isinstance cannot observe an external or dynamic object")
        return isinstance(value, allowed)

    def safe_enumerate(self, values, start=0):
        if type(start) is not int:
            raise Unsupported("enumerate start must be an integer")
        return list(enumerate(self.iterable(values), bounded(start)))

    def safe_zip(self, *values, strict=False):
        if type(strict) is not bool:
            raise Unsupported("zip strict must be a boolean")
        return list(zip(*(self.iterable(value) for value in values), strict=strict))

    def invoke(self, function, args, kwargs=None):
        self.tick()
        kwargs = kwargs or {}
        if type(function) is type and function in (list, tuple, dict, set, range, int, float, str, bool):
            if kwargs and function is not dict:
                raise Unsupported("unsupported safe builtin constructor keywords")
            for value in args:
                bounded(value)
            for value in kwargs.values():
                bounded(value)
            return bounded(function(*args, **kwargs))
        if isinstance(function, SafeCall):
            if self.callback_context and function.safe_tag not in {"pure_builtin", "pure_dependency", "read_only"}:
                raise Unsupported("key callback cannot perform I/O, randomness, or mutation")
            if (kwargs and (not function.keywords or (isinstance(function.keywords, tuple) and set(kwargs) - set(function.keywords)))) or (function.arity is not None and not function.arity[0] <= len(args) <= function.arity[1]):
                raise Unsupported("unsupported standard-library call signature")
            result = function.function(*args, **kwargs)
            return result if isinstance(result, File) else bounded(result)
        if isinstance(function, Class):
            self.allocate_object()
            value = Instance(function)
            initializer = function.fields.get("__init__")
            if initializer:
                if self.invoke(Function(initializer.node, initializer.defaults, value, initializer.kwdefaults), args, kwargs) is not None:
                    raise TypeError("__init__ must return None")
            elif args or kwargs:
                raise TypeError("Constructor takes no arguments")
            return value
        if not isinstance(function, Function):
            raise Unsupported("call target")
        stub = self.local_stubs.get(function.node.name) if function.node is self.stub_nodes.get(function.node.name) else None
        if stub is not None:
            count = self.stub_calls[function.node.name]
            if count >= stub["max_calls"]:
                raise ExecutionLimit("execution limit exceeded: stub call count for " + function.node.name)
            self.stub_calls[function.node.name] = count + 1
            outcome = stub["outcomes"][min(count, len(stub["outcomes"]) - 1)]
            if "raise" in outcome:
                raise ERRORS[outcome["raise"]]()
            return bounded(outcome["return"])
        parameters = function.node.args
        names = [arg.arg for arg in parameters.posonlyargs + parameters.args]
        keyword_names = [arg.arg for arg in parameters.kwonlyargs]
        supplied = ([function.owner] if function.owner is not None else []) + args
        if len(supplied) > len(names):
            raise TypeError("Too many arguments")
        values = dict(function.closure)
        values.update(zip(names, supplied))
        for name, value in kwargs.items():
            if name not in names + keyword_names or name in values or name in [arg.arg for arg in parameters.posonlyargs]:
                raise TypeError("Invalid keyword argument")
            values[name] = value
        for name, default in zip(names[len(names) - len(function.defaults):], function.defaults):
            values.setdefault(name, default)
        for name, default in function.kwdefaults.items():
            values.setdefault(name, default)
        if any(name not in values for name in names + keyword_names):
            raise TypeError("Missing argument")
        if self.depth >= 32:
            raise ExecutionLimit("execution limit exceeded: recursion depth")
        self.depth += 1
        try:
            self.block(function.node.body, values)
        except Returned as result:
            return result.value
        finally:
            self.depth -= 1

    def expr(self, node, env):
        self.tick()
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            return self.resolve(node.id)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.expr(item, env) for item in node.elts]
            return bounded(tuple(values) if isinstance(node, ast.Tuple) else set(values) if isinstance(node, ast.Set) else values)
        if isinstance(node, ast.Dict):
            return bounded({self.expr(key, env): self.expr(value, env) for key, value in zip(node.keys, node.values)})
        if isinstance(node, ast.Attribute):
            return self.attribute(self.expr(node.value, env), node.attr)
        if isinstance(node, ast.Subscript):
            value = self.expr(node.value, env)
            if self.dependencies.is_module(value):
                raise Unsupported("module subscripting")
            if type(value) not in (list, tuple, dict, str):
                raise Unsupported("subscript target")
            index = self.expr(node.slice, env)
            return value[index]
        if isinstance(node, ast.Slice):
            return slice(*(self.expr(part, env) if part else None for part in (node.lower, node.upper, node.step)))
        if isinstance(node, ast.Call):
            if any(keyword.arg is None for keyword in node.keywords):
                raise Unsupported("argument expansion")
            try:
                function = self.expr(node.func, env)
            except NameError:
                if not getattr(self, "module_mode", False):
                    name = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)[:80]
                    candidate = "candidate found but unavailable" if name in self.definitions else "no candidate found"
                    raise Unsupported(f"unresolved call target '{name}' in selected source and local dependency slice; {candidate}") from None
                raise
            return self.invoke(function, [self.expr(arg, env) for arg in node.args],
                               {keyword.arg: self.expr(keyword.value, env) for keyword in node.keywords})
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)):
            left, right = bounded(self.expr(node.left, env)), bounded(self.expr(node.right, env))
            if isinstance(node.op, ast.Pow):
                if type(left) not in (int, float) or type(right) is not int or abs(right) > 1000:
                    raise Unsupported("power requires a bounded numeric base and integer exponent")
                if left and math.log10(abs(left)) * right > 12:
                    raise Unsupported("numeric power limit")
                return bounded(operator.pow(left, right))
            if type(left) is not int or type(right) is not int:
                raise Unsupported("bit operations require bounded integers")
            if isinstance(node.op, ast.LShift) and right >= 0 and left and left.bit_length() + right > 40:
                raise Unsupported("integer shift size limit")
            operation = {ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor,
                         ast.LShift: operator.lshift, ast.RShift: operator.rshift}[type(node.op)]
            return bounded(operation(left, right))
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = self.expr(node.left, env), self.expr(node.right, env)
            bounded(left); bounded(right)
            if isinstance(node.op, ast.Add) and type(left) in (list, tuple, str) and type(right) is type(left) and len(left) + len(right) > (10000 if type(left) is str else 1000):
                raise Unsupported("sequence addition limit")
            if isinstance(node.op, ast.Mod) and type(left) not in (int, float, bool):
                raise Unsupported("string formatting is not supported")
            if isinstance(node.op, ast.Mult) and ((type(left) in (list, tuple, str) and type(right) is int and len(left) * right > 1000)
                                                 or (type(right) in (list, tuple, str) and type(left) is int and len(right) * left > 1000)):
                raise Unsupported("sequence multiplication limit")
            return bounded(OPS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd, ast.Not, ast.Invert):
            value = self.expr(node.operand, env)
            if isinstance(node.op, ast.Invert) and type(value) is not int:
                raise Unsupported("bit operations require bounded integers")
            return bounded({ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_, ast.Invert: operator.invert}[type(node.op)](value))
        if isinstance(node, ast.Compare):
            left = self.expr(node.left, env)
            for op, right_node in zip(node.ops, node.comparators):
                if type(op) not in COMPARE:
                    raise Unsupported("comparison")
                right = self.expr(right_node, env)
                if not COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            result = self.expr(node.values[0], env)
            for value in node.values[1:]:
                if (isinstance(node.op, ast.And) and not result) or (isinstance(node.op, ast.Or) and result):
                    break
                result = self.expr(value, env)
            return result
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test, env) else node.orelse, env)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            def generate(position, local):
                if position == len(node.generators):
                    if isinstance(node, ast.DictComp):
                        yield (self.expr(node.key, local), self.expr(node.value, local))
                    else:
                        yield self.expr(node.elt, local)
                    return
                clause = node.generators[position]
                if clause.is_async:
                    raise Unsupported("unsupported expression capability: Async comprehension")
                iterable = first_iterable if position == 0 else self.iterable(self.expr(clause.iter, local), charge=False)
                for item in iterable:
                    self.tick(iterations=1)
                    nested = dict(local)
                    self.assign(clause.target, item, nested)
                    if all(self.expr(condition, nested) for condition in clause.ifs):
                        yield from generate(position + 1, nested)
            # Python evaluates the outer iterable when creating a generator,
            # then evaluates its body lazily. Preserve short-circuit behavior.
            if node.generators[0].is_async:
                raise Unsupported("unsupported expression capability: Async comprehension")
            first_iterable = iter(self.iterable(self.expr(node.generators[0].iter, env), charge=False))
            iterator = generate(0, env if isinstance(node, ast.GeneratorExp) else dict(env))
            if isinstance(node, ast.GeneratorExp):
                self.allocate_object()
                return BoundedGenerator(iterator)
            values = list(iterator)
            if isinstance(node, ast.DictComp):
                return bounded(dict(values))
            if isinstance(node, ast.SetComp):
                return bounded(set(values))
            return bounded(values)
        if isinstance(node, ast.JoinedStr):
            return bounded("".join(str(self.expr(value, env)) if isinstance(value, ast.FormattedValue)
                                    else self.expr(value, env) for value in node.values))
        if isinstance(node, ast.FormattedValue):
            value = self.expr(node.value, env)
            if node.conversion == 114:
                value = repr(value)
            elif node.conversion == 97:
                value = ascii(value)
            elif node.conversion not in (-1, 115):
                raise Unsupported("unsupported expression capability: formatted value conversion")
            if node.format_spec:
                spec = self.expr(node.format_spec, env)
                if not isinstance(spec, str) or len(spec) > 100 or any(int(number) > 1000 for number in re.findall(r"\d+", spec)):
                    raise Unsupported("formatted value spec limit")
                return bounded(format(value, spec))
            return bounded(str(value))
        raise Unsupported("unsupported expression capability: " + type(node).__name__)

    def assign(self, target, value, env):
        self.tick()
        if self.callback_context and isinstance(target, (ast.Attribute, ast.Subscript)):
            raise Unsupported("key callback cannot mutate attributes or collections")
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)) and type(value) in (list, tuple, dict) and len(target.elts) == len(value):
            if type(value) is dict:
                value = tuple(value.keys())
            for item, part in zip(target.elts, value):
                self.assign(item, part, env)
        elif isinstance(target, ast.Attribute) and not target.attr.startswith("__"):
            owner = self.expr(target.value, env)
            if not isinstance(owner, (Instance, Class)):
                raise Unsupported("attribute mutation is restricted to interpreted local class state; read-only records and external objects cannot be mutated")
            if isinstance(value, Function):
                raise Unsupported("dynamic method assignment")
            owner.fields[target.attr] = value
        elif isinstance(target, ast.Subscript):
            owner = self.expr(target.value, env)
            if self.dependencies.is_module(owner):
                raise Unsupported("module mutation")
            if type(owner) not in (list, dict):
                raise Unsupported("subscript assignment")
            owner[self.expr(target.slice, env)] = value
            bounded(owner)
        else:
            raise Unsupported("assignment target")

    def block(self, statements, env):
        for node in statements:
            self.tick()
            if isinstance(node, ast.FunctionDef):
                if node.decorator_list or node.args.vararg or node.args.kwarg:
                    raise Unsupported("nested local callable signature/decorators are unsupported")
                if len(env) > MAX_LOCAL_BINDINGS:
                    raise ExecutionLimit("execution limit exceeded: local binding count")
                nested = self.define(node)
                nested.closure = {key: value for key, value in env.items() if
                                  type(value) in (int, float, str, bool, list, tuple, dict, set, type(None)) or
                                  isinstance(value, (Function, SafeCall, ReadOnlyRecord))}
                env[node.name] = nested
                continue
            if isinstance(node, ast.Return):
                raise Returned(self.expr(node.value, env) if node.value else None)
            if isinstance(node, ast.Expr):
                self.expr(node.value, env)
            elif isinstance(node, ast.Assign):
                value = self.expr(node.value, env)
                for target in node.targets:
                    self.assign(target, value, env)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self.assign(node.target, self.expr(node.value, env), env)
            elif isinstance(node, ast.AugAssign) and type(node.op) in OPS:
                # iadd preserves list aliasing, unlike a fresh binary addition.
                left, right = self.expr(node.target, env), self.expr(node.value, env)
                bounded(left); bounded(right)
                if self.callback_context and type(left) is list:
                    raise Unsupported("key callback cannot mutate collections")
                if isinstance(node.op, ast.Add) and type(left) is list:
                    if type(right) not in (list, tuple) or len(left) + len(right) > 1000:
                        raise Unsupported("sequence addition limit")
                    value = bounded(operator.iadd(left, right))
                else:
                    value = self.expr(ast.BinOp(left=node.target, op=node.op, right=node.value), env)
                self.assign(node.target, value, env)
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test, env) else node.orelse, env)
            elif isinstance(node, (ast.For, ast.While)):
                iterator = iter(self.iterable(self.expr(node.iter, env), charge=False)) if isinstance(node, ast.For) else None
                broken = False
                while True:
                    if iterator is not None:
                        try:
                            value = next(iterator)
                        except StopIteration:
                            break
                        self.assign(node.target, value, env)
                    elif not self.expr(node.test, env):
                        break
                    self.tick(iterations=1)
                    try:
                        self.block(node.body, env)
                    except BreakLoop:
                        broken = True
                        break
                    except ContinueLoop:
                        continue
                if not broken:
                    self.block(node.orelse, env)
            elif isinstance(node, ast.Break):
                raise BreakLoop()
            elif isinstance(node, ast.Continue):
                raise ContinueLoop()
            elif isinstance(node, ast.With):
                opened = []
                try:
                    for item in node.items:
                        file = self.expr(item.context_expr, env)
                        if not isinstance(file, File):
                            raise Unsupported("context manager")
                        opened.append(file)
                        if item.optional_vars:
                            self.assign(item.optional_vars, file, env)
                    self.block(node.body, env)
                finally:
                    for file in opened:
                        file.closed = True
            elif isinstance(node, ast.Try):
                try:
                    try:
                        self.block(node.body, env)
                    except tuple(ERRORS.values()) as error:
                        handled = False
                        for handler in node.handlers:
                            names = [] if handler.type is None else ([handler.type] if isinstance(handler.type, ast.Name) else handler.type.elts if isinstance(handler.type, ast.Tuple) else [])
                            if handler.type is not None and (not names or any(not isinstance(name, ast.Name) or name.id not in {*ERRORS, "Exception"} for name in names)):
                                raise Unsupported("exception handler type")
                            if handler.type is None or any(name.id == "Exception" or isinstance(error, ERRORS[name.id]) for name in names):
                                if handler.name:
                                    raise Unsupported("exception object inspection")
                                self.block(handler.body, env)
                                handled = True
                                break
                        if not handled:
                            raise
                    else:
                        self.block(node.orelse, env)
                finally:
                    self.block(node.finalbody, env)
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) and node.exc.func.id in ERRORS:
                raise ERRORS[node.exc.func.id](*[self.expr(arg, env) for arg in node.exc.args])
            elif isinstance(node, ast.Assert):
                if not self.expr(node.test, env):
                    raise AssertionError("Project assertion failed")
            elif isinstance(node, ast.Pass):
                continue
            else:
                raise Unsupported("unsupported statement capability: " + type(node).__name__)


def execute(source, symbol, spec):
    engine = Engine(source, spec)
    try:
        target = None if spec["kind"] in ("module_fragment", "verification_plan") else engine.resolve(symbol)
    except NameError:
        raise Unsupported("symbol is not defined locally") from None
    if spec["kind"] not in ("module_fragment", "verification_plan") and not isinstance(target, (Function, Class)):
        raise Unsupported("symbol is not a local function or class")
    print("ready", flush=True)
    if sys.stdin.readline().strip() != "run":
        raise Unsupported("missing execution handshake")
    args = spec.get("args", [])
    exception = None
    result = None
    try:
        if spec["kind"] == "module_fragment":
            result = engine.run_module_fragment(spec)
        elif spec["kind"] == "verification_plan":
            result = engine.run_plan(spec)
        elif spec["kind"] == "class_state_check":
            if not isinstance(target, Class):
                raise Unsupported("class symbol required")
            instances = [engine.invoke(target, constructor) for constructor in spec["constructors"]]
            for call in spec["calls"]:
                engine.invoke(engine.attribute(instances[call["instance"]], call["method"]), call.get("args", []))
            observe = spec["observe"]
            result = engine.attribute(instances[observe["instance"]], observe["attribute"])
        else:
            result = engine.invoke(target, args, spec.get("kwargs", {}))
        bounded(result)
    except tuple(ERRORS.values()) as error:
        exception = {"type": type(error).__name__, "message": str(error)[:500]}
        if spec["kind"] == "module_fragment":
            result = {"result": getattr(engine, "module_result", None),
                      "state": bounded(getattr(engine, "module_env", {})),
                      "stdout": "".join(engine.stdout), "stderr": "".join(engine.stderr),
                      "dependencies": dict(engine.dependency_classifications)}
    return {"result": result, "exception": exception, "args_after": bounded(args),
            "open_resources": sum(not file.closed for file in engine.files),
            "resources_created": len(engine.files), "random_draws": engine.draws,
            "stdout": "".join(engine.stdout), "stderr": "".join(engine.stderr)}


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.readline(200_000))
        observed = execute(payload["source"], payload["symbol"], payload["spec"])
        print(json.dumps({"observed": json_safe(observed)}, allow_nan=False), flush=True)
    except ExecutionLimit as error:
        print(json.dumps({"unsupported": str(error), "limit_exceeded": True}), flush=True)
    except Unsupported as error:
        print(json.dumps({"unsupported": str(error)}), flush=True)
    except Exception:
        # Never expose interpreter internals, raw source, or credentials in failures.
        print(json.dumps({"unsupported": "Source operation or verification input is outside the restricted engine"}), flush=True)
