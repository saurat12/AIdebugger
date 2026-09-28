"""Restricted AST worker. Launched with -I -S; never evals/imports target code."""

import ast
import json
import math
import operator
import random
import statistics
import sys


class Unsupported(Exception):
    pass


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
    def __init__(self, node, defaults, owner=None):
        self.node, self.defaults, self.owner = node, defaults, owner


class Class:
    def __init__(self):
        self.fields = {}


class Instance:
    def __init__(self, cls):
        self.cls, self.fields = cls, {}


class File:
    def __init__(self, content, mode):
        self.content, self.closed, self.mode = ("" if mode == "w" else content), False, mode
        self.position = len(content) if mode == "a" else 0


class SafeCall:
    def __init__(self, function, arity=None, keywords=False):
        self.function = function
        self.arity = arity
        self.keywords = keywords


class Stream:
    def __init__(self, name):
        self.name = name


ERRORS = {kind.__name__: kind for kind in (IndexError, ZeroDivisionError, ValueError, TypeError, KeyError, RuntimeError, AssertionError, NameError)}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


class Engine:
    def __init__(self, source, spec):
        self.spec = spec
        self.globals = {}
        self.files = []
        self.draws = []
        self.depth = 0
        self.instances_created = 0
        self.stdout = []
        self.stderr = []
        self.stdout_stream = Stream("stdout")
        self.stderr_stream = Stream("stderr")
        self.rng = random.Random(spec.get("seed", 0))
        self.stubs = {key: list(values) for key, values in spec.get("random_values", {}).items()}
        self.boundaries = {key: list(values) for key, values in spec.get("random_boundaries", {}).items()}
        self.random_module = object()
        self.builtins = {name: SafeCall(fn) for name, fn in {
            "len": len, "max": max, "min": min, "sum": sum, "abs": abs,
            "list": list, "tuple": tuple, "dict": dict, "sorted": sorted,
            "range": range, "enumerate": lambda seq, start=0: list(enumerate(seq, start)), "set": set,
            "all": all, "any": any, "zip": lambda *seqs: list(zip(*seqs)),
            "open": self.open_file,
        }.items()}
        self.builtins["print"] = SafeCall(self.print_values, keywords=True)
        self.builtins["open"].arity = (1, 2)
        tree = ast.parse(source)
        self.tree = tree
        if sum(1 for _ in ast.walk(tree)) > 5000:
            raise Unsupported("source node limit")
        self.definitions = {}
        self.binding_lines = {}
        self.loading = set()
        self.ambiguous = set()
        self.safe_modules = {
            "math": {name: SafeCall(getattr(math, name)) for name in ("sqrt", "floor", "ceil", "fabs", "isfinite", "isnan")},
            "statistics": {name: SafeCall(getattr(statistics, name)) for name in ("mean", "median")},
        }
        self.safe_modules["math"].update(pi=math.pi, e=math.e)
        self.safe_modules["sys"] = {"stdout": self.stdout_stream, "stderr": self.stderr_stream}
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

    def resolve(self, name):
        if name in self.ambiguous:
            raise Unsupported("required dependency has ambiguous or side-effectful initialization")
        if name in self.globals:
            return self.globals[name]
        if name in self.loading:
            raise Unsupported("cyclic initialization dependency")
        if name not in self.definitions:
            if name in self.builtins:
                return self.builtins[name]
            raise NameError(f"name '{name}' is not defined")
        self.loading.add(name)
        try:
            definition = self.definitions[name]
            if isinstance(definition, tuple):
                module, member = definition
                if module == "random":
                    value = self.random_module if member is None else self.attribute(self.random_module, member)
                elif module in self.safe_modules:
                    value = self.safe_modules[module] if member is None else self.safe_modules[module].get(member)
                    if value is None:
                        raise Unsupported("standard-library member is not allowlisted")
                else:
                    raise Unsupported("required import is not allowlisted")
            elif isinstance(definition, ast.FunctionDef):
                value = self.define(definition)
            elif isinstance(definition, ast.ClassDef):
                if definition.bases or definition.decorator_list or definition.keywords:
                    raise Unsupported("class inheritance/decorators")
                value = Class()
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
        finally:
            self.loading.remove(name)

    def safe_initializer(self, node, env):
        permitted = (ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple, ast.Dict,
                     ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
                     ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not)
        if any(not isinstance(child, permitted) for child in ast.walk(node)):
            raise Unsupported("required initializer is not a safe constant expression")
        return bounded(self.expr(node, env))

    @staticmethod
    def doc_or_pass(node):
        return isinstance(node, ast.Pass) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))

    def define(self, node):
        if node.decorator_list or node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            raise Unsupported("function signature/decorators")
        return Function(node, [self.safe_initializer(default, {}) for default in node.args.defaults])

    def open_file(self, name, mode="r"):
        if not isinstance(name, str) or name.startswith(("/", "\\")) or ":" in name or ".." in name.replace("\\", "/").split("/"):
            raise Unsupported("virtual resource path must be relative")
        if mode not in ("r", "w", "a") or name not in self.spec.get("files", {}):
            raise Unsupported("resource not explicitly supplied")
        if len(self.files) >= 100:
            raise Unsupported("resource limit")
        file = File(self.spec["files"][name], mode)
        self.files.append(file)
        return file

    def print_values(self, *values, sep=" ", end="\n", file=None):
        if not isinstance(sep, str) or not isinstance(end, str):
            raise Unsupported("print formatting")
        if file is not None and file not in (self.stdout_stream, self.stderr_stream):
            raise Unsupported("print stream")
        stream = self.stderr if file is self.stderr_stream else self.stdout
        text = sep.join(str(value) for value in values) + end
        if sum(map(len, stream)) + len(text) > 8000:
            raise Unsupported("captured stdout limit")
        stream.append(text)

    def run_plan(self, plan):
        instances, results, observed = {}, [], {}
        for operation in plan["steps"]:
            op = operation["op"]
            if op == "construct":
                cls = self.resolve(operation["symbol"])
                value = self.invoke(cls, operation.get("args", []), operation.get("kwargs", {}))
                instances[operation["as"]] = value
                result = value
            elif op == "call":
                target = operation["target"]
                if "." in target:
                    owner, member = target.split(".", 1)
                    if owner not in instances or not member.isidentifier() or member.startswith("_"):
                        raise Unsupported("plan call target")
                    callable_value = self.attribute(instances[owner], member)
                else:
                    if not target.isidentifier() or target.startswith("_"):
                        raise Unsupported("plan call target")
                    callable_value = self.resolve(target)
                call_args = operation.get("args", [])
                result = self.invoke(callable_value, call_args, operation.get("kwargs", {}))
                observed["args_after"] = bounded(call_args)
                bounded(result)
                results.append(result)
                if "as" in operation:
                    observed[operation["as"]] = result
            elif op == "observe":
                target = operation["target"]
                if "." not in target:
                    raise Unsupported("plan observation target")
                owner, member = target.split(".", 1)
                value = self.resolve(owner) if owner not in instances else instances[owner]
                observed[operation["as"]] = bounded(self.attribute(value, member))
            else:
                raise Unsupported("unknown plan operation")
        observed["last_result"] = results[-1] if results else None
        observed["return"] = observed["last_result"]
        observed["stdout"] = "".join(self.stdout)
        observed["stderr"] = "".join(self.stderr)
        observed["args_after"] = observed.get("args_after", [])
        observed["resources_open"] = sum(not file.closed for file in self.files)
        observed["resources_created"] = len(self.files)
        return observed

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
        return {"result": bounded(last), "state": bounded(env), "stdout": "".join(self.stdout), "stderr": "".join(self.stderr)}

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
        if name.startswith("_"):
            raise Unsupported("private/reflection attribute")
        for module in self.safe_modules.values():
            if value is module:
                if name not in module:
                    raise Unsupported("standard-library member is not allowlisted")
                return module[name]
        if value is self.random_module:
            if name not in ("randint", "randrange", "random"):
                raise Unsupported("random operation")
            arity = {"randint": (2, 2), "randrange": (1, 3), "random": (0, 0)}[name]
            return SafeCall(lambda *args: self.draw(name, *args), arity)
        if isinstance(value, (Instance, Class)):
            fields = value.fields
            if isinstance(value, Instance):
                result = fields[name] if name in fields else value.cls.fields.get(name)
            else:
                result = fields.get(name)
            if result is None and name not in fields and not (isinstance(value, Instance) and name in value.cls.fields):
                raise Unsupported("unknown state attribute")
            if isinstance(result, Function) and isinstance(value, Instance):
                return Function(result.node, result.defaults, value)
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
                result = getattr(value, name)(*args)
                bounded(value)
                return list(result) if name in ("keys", "values", "items") else result
            return SafeCall(method)
        raise Unsupported("attribute operation")

    def invoke(self, function, args, kwargs=None):
        kwargs = kwargs or {}
        if isinstance(function, SafeCall):
            if (kwargs and not function.keywords) or (function.arity is not None and not function.arity[0] <= len(args) <= function.arity[1]):
                raise Unsupported("unsupported standard-library call signature")
            result = function.function(*args, **kwargs)
            return result if isinstance(result, File) else bounded(result)
        if isinstance(function, Class):
            self.instances_created += 1
            if self.instances_created > 1000:
                raise Unsupported("instance allocation limit")
            value = Instance(function)
            initializer = function.fields.get("__init__")
            if initializer:
                if self.invoke(Function(initializer.node, initializer.defaults, value), args, kwargs) is not None:
                    raise TypeError("__init__ must return None")
            elif args or kwargs:
                raise TypeError("Constructor takes no arguments")
            return value
        if not isinstance(function, Function):
            raise Unsupported("call target")
        self.depth += 1
        if self.depth > 32:
            raise Unsupported("call depth limit")
        parameters = function.node.args
        names = [arg.arg for arg in parameters.posonlyargs + parameters.args]
        supplied = ([function.owner] if function.owner is not None else []) + args
        if len(supplied) > len(names):
            raise TypeError("Too many arguments")
        values = dict(zip(names, supplied))
        for name, value in kwargs.items():
            if name not in names or name in values or name in [arg.arg for arg in parameters.posonlyargs]:
                raise TypeError("Invalid keyword argument")
            values[name] = value
        for name, default in zip(names[len(names) - len(function.defaults):], function.defaults):
            values.setdefault(name, default)
        if any(name not in values for name in names):
            raise TypeError("Missing argument")
        try:
            self.block(function.node.body, values)
        except Returned as result:
            return result.value
        finally:
            self.depth -= 1

    def expr(self, node, env):
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
            if any(value is module for module in self.safe_modules.values()):
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
                    raise Unsupported("unsupported expression capability: unresolved call target") from None
                raise
            return self.invoke(function, [self.expr(arg, env) for arg in node.args],
                               {keyword.arg: self.expr(keyword.value, env) for keyword in node.keywords})
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = self.expr(node.left, env), self.expr(node.right, env)
            bounded(left); bounded(right)
            if isinstance(node.op, ast.Mod) and type(left) not in (int, float, bool):
                raise Unsupported("string formatting is not supported")
            if isinstance(node.op, ast.Mult) and ((type(left) in (list, tuple, str) and type(right) is int and len(left) * right > 1000)
                                                 or (type(right) in (list, tuple, str) and type(left) is int and len(right) * left > 1000)):
                raise Unsupported("sequence multiplication limit")
            return bounded(OPS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd, ast.Not):
            return bounded({ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}[type(node.op)](self.expr(node.operand, env)))
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
            values = []
            def generate(position, local):
                if position == len(node.generators):
                    if isinstance(node, ast.DictComp):
                        values.append((self.expr(node.key, local), self.expr(node.value, local)))
                    else:
                        values.append(self.expr(node.elt, local))
                    if len(values) > 1000:
                        raise Unsupported("comprehension result limit")
                    return
                clause = node.generators[position]
                if clause.is_async:
                    raise Unsupported("unsupported expression capability: Async comprehension")
                iterable = self.expr(clause.iter, local)
                if type(iterable) not in (list, tuple, range, str, dict):
                    raise Unsupported("unsupported expression capability: comprehension iterable")
                if len(iterable) > 1000:
                    raise Unsupported("comprehension iteration limit")
                for item in iterable:
                    nested = dict(local)
                    self.assign(clause.target, item, nested)
                    if all(self.expr(condition, nested) for condition in clause.ifs):
                        generate(position + 1, nested)
            generate(0, dict(env))
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
                if not isinstance(spec, str) or len(spec) > 100:
                    raise Unsupported("formatted value spec limit")
                return format(value, spec)
            return str(value)
        raise Unsupported("unsupported expression capability: " + type(node).__name__)

    def assign(self, target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)) and type(value) in (list, tuple, dict) and len(target.elts) == len(value):
            if type(value) is dict:
                value = tuple(value.keys())
            for item, part in zip(target.elts, value):
                self.assign(item, part, env)
        elif isinstance(target, ast.Attribute) and not target.attr.startswith("_"):
            owner = self.expr(target.value, env)
            if not isinstance(owner, (Instance, Class)):
                raise Unsupported("attribute assignment")
            if isinstance(value, Function):
                raise Unsupported("dynamic method assignment")
            owner.fields[target.attr] = value
        elif isinstance(target, ast.Subscript):
            owner = self.expr(target.value, env)
            if any(owner is module for module in self.safe_modules.values()):
                raise Unsupported("module mutation")
            if type(owner) not in (list, dict):
                raise Unsupported("subscript assignment")
            owner[self.expr(target.slice, env)] = value
            bounded(owner)
        else:
            raise Unsupported("assignment target")

    def block(self, statements, env):
        for node in statements:
            if isinstance(node, ast.Return):
                raise Returned(self.expr(node.value, env) if node.value else None)
            if isinstance(node, ast.Expr):
                self.expr(node.value, env)
            elif isinstance(node, ast.Assign):
                value = self.expr(node.value, env)
                for target in node.targets:
                    self.assign(target, value, env)
            elif isinstance(node, ast.AugAssign) and type(node.op) in OPS:
                # iadd preserves list aliasing, unlike a fresh binary addition.
                left, right = self.expr(node.target, env), self.expr(node.value, env)
                bounded(left); bounded(right)
                if isinstance(node.op, ast.Add) and type(left) is list:
                    value = bounded(operator.iadd(left, right))
                else:
                    value = self.expr(ast.BinOp(left=node.target, op=node.op, right=node.value), env)
                self.assign(node.target, value, env)
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test, env) else node.orelse, env)
            elif isinstance(node, (ast.For, ast.While)):
                iterator = iter(bounded(self.expr(node.iter, env))) if isinstance(node, ast.For) else None
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
        target = None if spec["kind"] == "module_fragment" else engine.resolve(symbol)
    except NameError:
        raise Unsupported("symbol is not defined locally") from None
    if spec["kind"] != "module_fragment" and not isinstance(target, (Function, Class)):
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
                      "stdout": "".join(engine.stdout), "stderr": "".join(engine.stderr)}
    return {"result": result, "exception": exception, "args_after": bounded(args),
            "open_resources": sum(not file.closed for file in engine.files),
            "resources_created": len(engine.files), "random_draws": engine.draws,
            "stdout": "".join(engine.stdout), "stderr": "".join(engine.stderr)}


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.readline(200_000))
        observed = execute(payload["source"], payload["symbol"], payload["spec"])
        print(json.dumps({"observed": json_safe(observed)}, allow_nan=False), flush=True)
    except Unsupported as error:
        print(json.dumps({"unsupported": str(error)}), flush=True)
    except Exception:
        # Never expose interpreter internals, raw source, or credentials in failures.
        print(json.dumps({"unsupported": "Source operation or verification input is outside the restricted engine"}), flush=True)
