# Copyright (c) 2026 Chrys. All rights reserved.

# Ported from Chrys 6fcfb332, with generated-command validation from 0561cf0f:
# src/chrys/service/requirement_clarification/verification.py; execution is JiuwenSwarm.
"""Public checks synthesized from clarified requirements, separate from PACT goals."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VERIFICATION_FILE = "public-verification.json"


class PublicCheck(BaseModel):
    """One repeatable command and the acceptance criteria it is intended to exercise."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=120)
    command: NonBlank = Field(max_length=16000)
    target_ac_ids: list[NonBlank] = Field(min_length=1, max_length=1)
    rationale: NonBlank = Field(max_length=4000)


class PublicVerification(BaseModel):
    """A generated public verifier is a proposal, never the independent grading oracle."""

    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["jiuwenswarm/pact-public-verification/v1"] = Field(alias="schema")
    status: Literal["ready", "unavailable"]
    reason: NonBlank = Field(max_length=4000)
    checks: list[PublicCheck] = Field(max_length=100)

    def validate_coverage(self, acceptance_ids: set[str]) -> None:
        """Refuse empty/unavailable proposals and foreign or missing criterion references."""
        if self.status != "ready" or not self.checks:
            raise ValueError("public verification unavailable: " + self.reason)
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("public verification check IDs must be unique")
        covered: set[str] = set()
        for check in self.checks:
            if "\x00" in check.command:
                raise ValueError("public verification commands cannot contain NUL")
            if len(check.target_ac_ids) != len(set(check.target_ac_ids)):
                raise ValueError("public verification criterion references must be unique")
            covered.update(check.target_ac_ids)
        if covered != acceptance_ids:
            raise ValueError(
                "public verification must reference exactly the Goal Contract's acceptance criteria: "
                f"missing={sorted(acceptance_ids - covered)}, foreign={sorted(covered - acceptance_ids)}"
            )

    def validate_generated_commands(self) -> None:
        """Reject observed non-verifiers; actual execution remains mandatory."""
        for check in self.checks:
            if check.command.strip() in {"true", ":", "exit 0", "PLACEHOLDER", "TODO", "TBD"}:
                raise ValueError(f"check {check.id} contains no executable behavioral assertion")
            if _suppresses_check_failure(check.command):
                raise ValueError(f"check {check.id} suppresses failure with an unconditional success fallback")
            if re.match(r"(?i)^placeholder\b|^to be replaced\b", check.rationale):
                raise ValueError(f"check {check.id} is an unfinished placeholder")
            if "DSML" in check.command:
                raise ValueError(f"check {check.id} contains tool-call markup instead of an executable command")
            _validate_literal_regex_setup(check.command)


_SHELL_COMMENT = re.compile(r"""'(?:[^']*)'|"(?:\\.|[^"\\])*"|\\.|(?P<comment>(?:^|(?<=[\s;|&]))\#.*$)""", re.MULTILINE)


_RAW_FALLBACK = re.compile(r"\|\|\s*(?:true|:)\s*;?\s*$", re.MULTILINE)


def _shell_fallback_lines(command: str) -> list[list[str]] | None:
    """Tokenize literal shell lines while keeping quoted words and skipping data heredocs."""
    lines = command.strip().splitlines()
    wrapper = _SHELL_SCRIPT.fullmatch(lines[0].strip())
    if wrapper and lines[-1] == wrapper[2]:
        lines = lines[1:-1]
    result = []
    shell_consumer = re.search(
        r"(?m)(?:^|[;|&])[ \t]*(?:/bin/)?(?:sh|bash|eval|source|\.|\./[^ ]+)\s", "\n".join(lines)
    )
    delimiters = []
    continued = ""
    for line in lines:
        if delimiters:
            end, strip_tabs = delimiters[0]
            if (line.lstrip("\t") if strip_tabs else line) == end:
                delimiters.pop(0)
            continue
        if not continued and line.lstrip().startswith("#"):
            continue
        logical = continued + line
        source = _SHELL_COMMENT.sub(lambda m: "" if m["comment"] else m[0], logical)
        lexer = shlex.shlex(source, posix=False, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        # A # within a shell word (for example a path) is ordinary data.
        lexer.commenters = ""
        try:
            words = list(lexer)
        except ValueError:
            continued = logical + "\n"
            continue
        continued = ""
        if words and words[-1] == ";":
            words.pop()
        if not words:
            continue
        if result and result[-1][-1] == "||":
            words = result.pop() + words
        # Split a simple trailing statement ("probe || true; echo done").
        # Compound shell syntax remains subject to the line-level guard.
        if ";" in words:
            cut = words.index(";")
            if words[cut - 2 : cut] in (["||", "true"], ["||", ":"]):
                result.append(words[:cut])
                words = words[cut + 1 :]
        if not words:
            continue
        result.append(words)
        # Literal contents of a data file or an interpreted Python probe are
        # not outer-shell commands. Leave other shell-launching forms visible.
        if not {"|", "||", "&&", ";"}.intersection(words) and (
            words[0] == "cat" and not shell_consumer or re.fullmatch(r"(?:\w+=\$\()?python(?:3(?:\.\d+)?)?", words[0])
        ):
            for i, word in enumerate(words[:-1]):
                if word == "<<":
                    match = re.fullmatch(r"""(-?)(['"]?)(\w+)\2""", words[i + 1])
                    if match and match[2]:
                        delimiters.append((match[3], bool(match[1])))
    return None if continued or delimiters else result


# An EXIT trap can replace the final status. Permit only cleanup commands with
# simple arguments, including a named function made entirely of those commands.
_CLEANUP = re.compile(
    r"""(?:if \[ -[nz] ["'\w$./-]+ \] ; then )?(?:kill|rm|rmdir|wait)(?: ["'\w$./-]+)*"""
    r"""(?: 2 > /dev/null)?(?: \|\| (?:true|:))?(?: ; fi)?"""
)


def _unknown_exit_trap(lines: list[list[str]]) -> bool:
    for words in lines:
        if "trap" not in words:
            continue
        if len(words) != 3 or words[0] != "trap" or words[2] not in {"0", "EXIT"}:
            return True
        handler = words[1].strip("'\"")
        if _CLEANUP.fullmatch(handler) or handler in {"-", ""}:
            continue
        definitions = [i for i, row in enumerate(lines) if row == [handler + "()", "{"]]
        if len(definitions) != 1 or ["}"] not in lines[definitions[0] + 1 :]:
            return True
        start = definitions[0] + 1
        if not all(_CLEANUP.fullmatch(" ".join(row)) for row in lines[start : lines.index(["}"], start)]):
            return True
    return False


def _suppresses_check_failure(command: str) -> bool:
    """Recognize discarded exit statuses and a check whose verdict comes from captured output."""
    if "||" not in command:
        return False
    if re.search(r"(?m)^\s*eval\s", command) and _RAW_FALLBACK.search(command):
        return True
    lines = _shell_fallback_lines(command)
    if lines is None:
        return _RAW_FALLBACK.search(command) is not None
    masked = {i for i, w in enumerate(lines) if w[-2:] in (["||", "true"], ["||", ":"])}
    if not masked:
        return False
    if _unknown_exit_trap(lines):
        return True
    if len(lines[-1]) != 2 or lines[-1][0] != "exit" or not re.fullmatch(r"[1-9]\d?", lines[-1][1]):
        return True

    def diagnostic(words: list[str]) -> bool:
        return bool(words) and words[0] in {"cat", "echo", "printf"} and not {";", "&&", "||", "|"}.intersection(words)

    # Best-effort logs on the path ending in explicit failure cannot produce a pass.
    cursor = len(lines) - 2
    while cursor >= 0 and diagnostic(lines[cursor][:-2] if cursor in masked else lines[cursor]):
        masked.discard(cursor)
        cursor -= 1
    if not masked:
        return False
    if cursor < 3 or lines[cursor] != ["fi"] or lines[cursor - 1] != ["exit", "0"]:
        return True
    cursor -= 2
    while cursor >= 0 and diagnostic(lines[cursor]):
        cursor -= 1
    if cursor < 1 or masked != {cursor - 1}:
        return True
    predicate = lines[cursor]
    if not (
        len(predicate) == 7
        and predicate[:2] == ["if", "grep"]
        and predicate[2] in {"-q", "-Fq", "-qF"}
        and predicate[-2:] == [";", "then"]
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", predicate[3].strip("'\""))
    ):
        return True
    # The producer must freshly truncate this same output. Appends, alternate
    # paths, pipelines and short-circuited commands do not establish that link.
    producer = lines[cursor - 1][:-2]
    marker = predicate[3].strip("'\"")
    if (
        not producer
        or producer[0] in {"cat", "echo", "printf", "true", ":"}
        or any(marker in token for token in producer)
        or {";", "&&", "||", "|", ">>"}.intersection(producer)
    ):
        return True
    outputs = [
        producer[i + 1]
        for i, word in enumerate(producer[:-1])
        if word == ">" and i > 0 and (not producer[i - 1].isdigit() or producer[i - 1] == "1")
    ]
    return len(outputs) != 1 or outputs[0] != predicate[4]


def json_digest(value: object) -> str:
    """Hash semantic JSON independently of artifact indentation."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


_PYTHON_HEREDOC = re.compile(
    r"(?m)^(?P<header>python(?:3(?:\.\d+)?)?\b[^\n]*<<(?P<quote>['\"]?)"
    r"(?P<tag>[A-Za-z_][A-Za-z_0-9]*)(?P=quote)[ \t]*\n)(?P<body>[\s\S]*?)^(?P=tag)[ \t]*$"
)
_PYTHON_STDIN = re.compile(r"""python3?(?:[ \t]+-)?[ \t]*<<[ \t]*(['"])(\w+)\1""")


# Accept a final, literal Python stdin invocation. Its arguments are data, and
# the quoted delimiter prevents the shell from rewriting the program.
_PYTHON_STDIN_ARGS = re.compile(
    r"""python3?[ \t]+-(?:[ \t]+"\$[A-Za-z_][A-Za-z_0-9]*")*[ \t]*<<[ \t]*(['"])(\w+)\1"""
)


def _straight_shell_setup(prefix: list[str]) -> bool:
    """Only ordinary commands, assignments, cleanup, and failure-only guards."""
    if not any(line.strip() for line in prefix):
        return True
    rows = _shell_fallback_lines("\n".join(prefix))
    if rows is None or _unknown_exit_trap(rows) or any("<<" in row or row.endswith("\\") for row in prefix):
        return False
    controls = {";", "&&", "||", "|", "&", "(", ")", "{", "}"}
    for row in rows:
        if row[0] == "trap" or row == ["set", "-e"]:
            continue
        if "||" in row:
            # [ predicate ] || { ...; exit 1; } either continues or fails.
            pos = row.index("||")
            if not (
                row[0] == "[" and row[pos - 1] == "]"
                and row[pos + 1:pos + 3] in (["{", "echo"], ["{", "printf"])
                and row[-5:] == [";", "exit", "1", ";", "}"]
                and not controls.intersection(row[pos + 3:-5])
            ):
                return False
            row = row[:pos]
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=\$\([A-Za-z_/][A-Za-z_0-9/-]*\)", "".join(row)):
            continue
        if row[0] == "[":
            if row[-1] != "]":
                return False
        elif (not re.fullmatch(r"[A-Za-z_][\w.-]*", row[0])
              or row[0] in {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case",
                            "esac", "function", "return", "exit", "exec", "eval", "alias", "source", "builtin",
                            "command", "break", "continue", "enable", "export", "unset", "readonly", "set", "shopt"}):
            return False
        if controls.intersection(row):
            return False
    return True


def _requires_nonempty(statement: ast.stmt | None, name: str, sys_imported: bool) -> bool:
    """The empty comprehension must also fail for this to be a broken check."""
    match statement:
        case ast.Assert(test=ast.Name(id=target)):
            return target == name
        case ast.If(test=ast.UnaryOp(op=ast.Not(), operand=ast.Name(id=target)), body=[ast.Expr(value=call)]):
            if target != name or not sys_imported:
                return False
            match call:
                case ast.Call(func=ast.Attribute(value=ast.Name(id="sys"), attr="exit"),
                              args=[ast.Constant(value=status)], keywords=[]):
                    return isinstance(status, str) or type(status) is int and 0 < status < 256
    return False


def _validate_match_groups(statement: ast.stmt, following: ast.stmt | None, sys_imported: bool, header: int) -> None:
    """A single eager comprehension binds the stdlib Match for all its elements."""
    match statement:
        case ast.Assign(targets=[ast.Name(id=result)], value=ast.ListComp(elt=elt, generators=[ast.comprehension(
            target=ast.Name(id=match_name), ifs=[], is_async=0, iter=ast.Call(
                func=ast.Attribute(value=ast.Name(id="re"), attr="finditer"),
                args=[ast.Constant(value=pattern), _], keywords=[])
        )])):
            pass
        case _:
            return
    if not isinstance(pattern, (str, bytes)) or not _requires_nonempty(following, result, sys_imported):
        return
    try:
        compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError):
        return
    expressions = [elt]
    while expressions:
        item = expressions.pop()
        match item:
            case ast.Tuple(elts=values) | ast.List(elts=values):
                expressions.extend(values)
            case ast.Call(func=ast.Attribute(value=ast.Name(id=receiver), attr="group"), args=args, keywords=[]) if receiver == match_name:
                for arg in args:
                    if not isinstance(arg, ast.Constant):
                        continue
                    key = arg.value
                    if (type(key) is int and not 0 <= key <= compiled.groups
                            or isinstance(key, str) and key not in compiled.groupindex):
                        raise ValueError(
                            f"invalid Python capture group {key!r} at line {item.lineno} "
                            f"(command line {header + item.lineno}): pattern has {compiled.groups} capture groups"
                        )
            case ast.Call(args=args, keywords=keywords):
                # Function arguments are eager. Conditional expressions, lambda
                # bodies, nested scopes and short-circuit operators are opaque.
                expressions.extend(args)
                expressions.extend(k.value for k in keywords)


def _validate_first_match_comprehension(tree: ast.Module, header: int) -> None:
    """Prove the module binding only across imports and passive input setup."""
    modules: set[str] = set()
    for index, statement in enumerate(tree.body):
        match statement:
            case ast.Import(names=names) if all(n.name in {"re", "sys"} and n.asname is None for n in names):
                modules.update(n.name for n in names)
                continue
            case ast.ImportFrom(module="pathlib", level=0, names=[ast.alias(name="Path", asname=None)]):
                modules.add("Path")
                continue
            case ast.Expr(value=ast.Constant(value=str())) | ast.Pass():
                continue
            case ast.Assign(targets=[ast.Name(id=target)], value=value) if target not in {"re", "sys", "Path"}:
                if isinstance(value, (ast.Constant, ast.Name)):
                    continue
                # A literal Path or an argv entry may supply the input text.
                match value:
                    case ast.Call(func=ast.Attribute(value=ast.Call(func=ast.Name(id="Path"), args=[path],
                                      keywords=[]), attr=("read_text" | "read_bytes")), args=[], keywords=opts):
                        if "Path" in modules and all(isinstance(opt.value, ast.Constant) for opt in opts):
                            match path:
                                case ast.Constant(value=str()):
                                    continue
                                case ast.Subscript(value=ast.Attribute(value=ast.Name(id="sys"), attr="argv"),
                                                   slice=ast.Constant(value=int())) if "sys" in modules:
                                    continue
        if "re" in modules:
            following = tree.body[index + 1] if index + 1 < len(tree.body) else None
            _validate_match_groups(statement, following, "sys" in modules, header)
        # Unknown setup may change re.finditer, sys.exit, or terminate before a
        # later statement. Never carry the proof of their identity across it.
        return


def _validate_literal_regex_setup(command: str) -> None:
    """Check literal regex setup and capture accesses in executable Python probes."""
    lines = command.strip().splitlines()
    if not lines or not ("re.compile" in command or "re.finditer" in command and ".group(" in command):
        return
    header = _PYTHON_STDIN.fullmatch(lines[0])
    standalone = header is not None
    start = 0
    if not standalone:
        for start, line in enumerate(lines):
            header = _PYTHON_STDIN_ARGS.fullmatch(line)
            if header:
                break
        if not header or not _straight_shell_setup(lines[:start]):
            return
    if lines[-1] != header[2] or header[2] in lines[start + 1:-1]:
        return
    try:
        tree = ast.parse("\n".join(lines[start + 1:-1]))
    except (SyntaxError, ValueError, RecursionError):
        return

    _validate_first_match_comprehension(tree, start + 1)
    if not standalone:
        return
    imported = False
    exit_names = {"exit", "quit", "_exit", "SystemExit"}
    for statement in tree.body:
        nodes = list(ast.walk(statement))
        # A successful early exit can make a later literal a compatibility
        # fallback. Only direct exits with known failure status may be ignored.
        failing_exits = {
            n.func
            for n in nodes
            if isinstance(n, ast.Call)
            and len(n.args) == 1
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, int)
            and 0 < n.args[0].value < 256
            and not n.keywords
        }
        if any(
            (
                isinstance(n, ast.Name)
                and n.id in exit_names
                or isinstance(n, ast.Attribute)
                and n.attr in exit_names
                or isinstance(n, ast.alias)
                and n.name in exit_names
            )
            and n not in failing_exits
            for n in nodes
        ):
            return
        if isinstance(statement, ast.Import) and [(a.name, a.asname) for a in statement.names] == [("re", None)]:
            imported = True
            continue
        if not imported:
            continue
        # Stay within ordinary stdlib use. A rebound module, patched method, or
        # namespace passed to a helper can have different pattern semantics.
        receivers = {n.value for n in nodes if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)}
        if any(
            (isinstance(n, ast.Name) and n.id == "re" and n not in receivers)
            or (isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) in {"re", "*"})
            or (isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "re")
            for n in nodes
        ):
            return
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            continue
        call = statement.value
        match call:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="re"), attr="compile"),
                args=[ast.Constant(value=pattern)],
                keywords=[],
            ) if isinstance(pattern, (str, bytes)):
                pass
            case _:
                continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid Python regular expression in probe setup at line {call.lineno}: {exc}") from exc
        except (OverflowError, RecursionError):
            pass


_DATA_PROBE_MODULES = frozenset({"os", "re", "sys", "json", "pathlib", "shlex", "hashlib", "typing", "collections"})
_DYNAMIC_EXECUTION_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "__builtins__", "getattr", "globals", "locals", "vars"}
)
_EXECUTION_ATTRIBUTES = frozenset(
    {"system", "popen", "Popen", "run", "call", "check_call", "check_output", "run_path", "__getattribute__"}
)


def referenced_public_tests(command: str) -> set[str]:
    """Keep test dispatch references, without treating inline data fixtures as test invocations.

    This is an evidence guard, not a shell/Python security analyzer. Unknown heredoc
    shapes, parse failures and scripts using dispatch/reflection retain the previous
    conservative whole-script check. A simple artifact parser may legitimately use
    nonexistent test filenames in synthetic diff fixtures without executing them.
    """

    def exclude_data_program(match: re.Match[str]) -> str:
        try:
            tree = ast.parse(match["body"])
        except (SyntaxError, ValueError, RecursionError):
            return match[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".", 1)[0] not in _DATA_PROBE_MODULES for alias in node.names
            ):
                return match[0]
            if isinstance(node, ast.ImportFrom):
                if node.level or (node.module or "").split(".", 1)[0] not in _DATA_PROBE_MODULES:
                    return match[0]
                if any(
                    alias.name == "*" or alias.name in _EXECUTION_ATTRIBUTES or alias.name.startswith(("exec", "spawn"))
                    for alias in node.names
                ):
                    return match[0]
            if isinstance(node, ast.Name) and node.id in _DYNAMIC_EXECUTION_NAMES:
                return match[0]
            if isinstance(node, ast.Attribute) and (
                node.attr in _EXECUTION_ATTRIBUTES or node.attr.startswith(("exec", "spawn"))
            ):
                return match[0]
        return match["header"] + match["tag"]

    inspected_command = _PYTHON_HEREDOC.sub(exclude_data_program, command)
    return set(re.findall(r"\btests?/[A-Za-z0-9_./-]+\.py\b", inspected_command))


_SHELL_SCRIPT = re.compile(r"""(?:/bin/)?(?:bash|sh)\s+<<\s*(['"])(\w+)\1""")
_WORKSPACE_NAME = r"(?:CHRYS|JIUWENSWARM)_PACT_SOURCE_WORKSPACE"
_REQUIRED_WORKSPACE = re.compile(rf""":\s+"?\$\{{({_WORKSPACE_NAME}):?\?[^}}"$`]*\}}"?(?:\s+#.*)?""")
# These forms have no shell-side effects before a subsequent explicit requirement.
# Stop inspecting at any other shell syntax. The native review and runner handle it.
_NAME = r"[A-Za-z_][A-Za-z_0-9]*"
_LITERAL = r"""(?:[A-Za-z0-9_./-]+|"[A-Za-z0-9_./-]*"|'[A-Za-z0-9_./-]*'|)"""
_OUTPUT_WORD = r"""(?:'[^']*'|"[^"\\`]*"|[A-Za-z0-9_./%-]+)"""
_GIT_CWD = rf"""(?:-C "\$(?P<cwd>{_NAME})" )?"""
_GIT_READ = re.compile(
    rf'''{_NAME}=(?P<quote>"?)\$\((?:pwd(?: -P)?|git {_GIT_CWD}(?:rev-parse|status)(?: [A-Za-z0-9_./=-]+)*)\)(?P=quote)'''
)
_BRANCH_TEST = re.compile(
    rf"""if (?P<assigned>{_NAME})="\$\(git {_GIT_CWD}symbolic-ref (?:(?:--short|-q|--quiet) )*HEAD(?: 2>/dev/null)?\)"; then"""
)
_ASSIGNMENT = re.compile(rf"""(?:export )?({_NAME})=({_LITERAL}|"\$({_NAME})")""")
_DATA_HEREDOC = re.compile(r"""cat <<(-?)[ \t]*(['"])(\w+)\2""")
_EMPTY_DEFAULT = re.compile(
    rf'''(?:export )?({_NAME})=(?P<quote>"?)\$\{{({_NAME}):?-\}}(?P=quote)'''
)
_ABORT_TEST = re.compile(
    rf"""(.+?)\s+\|\|\s+(?:exit [1-9]|\{{ (?:echo {_OUTPUT_WORD}(?: >&2)?; )?exit [1-9]; \}})"""
)
_READ_ONLY_TEST = re.compile(
    r"""(?:\[ -[fde] [\w./-]+ \]|test -[fde] [\w./-]+|test -z "\$\(git status --porcelain(?: --untracked-files=no)?\)")"""
)


def validate_public_workspace_dependencies(command: str) -> None:
    """Reject explicit requirements provable in a simple, read-only shell preamble."""
    if "_PACT_SOURCE_WORKSPACE" not in command:
        return
    lines = command.splitlines()
    wrapper = _SHELL_SCRIPT.fullmatch(lines[0].strip())
    if wrapper and lines[-1] == wrapper[2]:
        lines = lines[1:-1]
    missing = {"CHRYS_PACT_SOURCE_WORKSPACE", "JIUWENSWARM_PACT_SOURCE_WORKSPACE"}
    empty_aliases: dict[str, str] = {}
    candidate_dirs = {"PWD"}
    # An ordinary test can take either branch. An attached-HEAD test on the
    # unmodified direct candidate always takes its else branch.
    branch = None
    script = iter(lines)
    for raw_line in script:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "fi" and branch:
            branch = None
            continue
        if line == "else" and branch == "attached":
            branch = "detached"
            continue
        output = re.fullmatch(rf"(?:echo|printf '%s(?:\\n)?')(?: +{_OUTPUT_WORD})*", line)
        if output and "$(" not in line and "${" not in line:
            continue
        if branch in ("test", "attached"):
            if re.fullmatch(r"exit [1-9]", line):
                continue
            return
        if re.fullmatch(r"if \[ [\w $\"!=./-]+ \]; then", line) and branch is None:
            branch = "test"
            continue
        condition = _BRANCH_TEST.fullmatch(line)
        if condition and branch is None:
            if condition["cwd"] is not None and condition["cwd"] not in candidate_dirs:
                return
            if condition["assigned"] in missing | candidate_dirs or condition["assigned"].startswith("GIT_"):
                return
            branch = "attached"
            continue
        data = _DATA_HEREDOC.fullmatch(line)
        if data:
            for body_line in script:
                if (body_line.lstrip("\t") if data[1] else body_line) == data[3]:
                    break
            continue
        required = _REQUIRED_WORKSPACE.fullmatch(line)
        if required:
            if required[1] in missing:
                raise ValueError(
                    f"generated command requires {required[1]}, which this verification runtime does not supply"
                )
            continue
        if re.fullmatch(r"set -[eu]+(?:o pipefail)?", line):
            continue
        if re.fullmatch(r"git rev-parse --is-inside-work-tree(?: >/dev/null)?", line):
            continue
        abort = _ABORT_TEST.fullmatch(line)
        if abort:
            predicate = abort[1]
            if _READ_ONLY_TEST.fullmatch(predicate):
                continue
            nonempty = re.fullmatch(rf'''(?:\[ -n "\$({_NAME})" \]|test -n "\$({_NAME})")''', predicate)
            if nonempty:
                name = nonempty[1] or nonempty[2]
                source = empty_aliases.get(name, name)
                if name in empty_aliases or name in missing:
                    raise ValueError(f"generated command requires {source}, which this verification runtime does not supply")
                continue
            return
        copy = _EMPTY_DEFAULT.fullmatch(line)
        assigned = _ASSIGNMENT.fullmatch(line)
        if assigned or copy or _GIT_READ.fullmatch(line):
            assignment = assigned or copy
            key = assignment[1] if assignment else line.partition("=")[0]
            if key in {"PATH", "ENV", "BASH_ENV"} or key.startswith("GIT_"):
                return
            source = assignment[3] if assignment else None
            unavailable = empty_aliases.get(source, source if source in missing else None)
            missing.discard(key)
            empty_aliases.pop(key, None)
            # Special shell names can compute their own value on expansion.
            if unavailable and key != key.upper():
                empty_aliases[key] = unavailable
            if assigned and assigned[3] in candidate_dirs:
                candidate_dirs.add(key)
            else:
                candidate_dirs.discard(key)
            continue
        default = re.fullmatch(rf''': "\$\{{({_WORKSPACE_NAME}):=[A-Za-z0-9_./-]+\}}"''', line)
        if default:
            missing.discard(default[1])
            continue
        unset = re.fullmatch(rf"unset ({_WORKSPACE_NAME})", line)
        if unset:
            missing.add(unset[1])
            continue
        if re.fullmatch(rf"{_WORKSPACE_NAME}={_LITERAL} env(?: >/dev/null)?", line):
            continue
        return


PUBLIC_VERIFICATION_INSTRUCTIONS = """Generate repeatable public verification commands from the original requirement,
validated clarification, immutable Goal Contract and Initial Plan. Inspect public tests, build configuration and
documentation in the frozen repository with the read-only tools. The commands will run from a fresh checkout of a
candidate revision, not this snapshot: never embed snapshot paths, session paths or implementation-specific answers.
The candidate is a CLEAN COMMITTED checkout: git diff HEAD and git diff are normally empty even for a correct change.
Treat a source-workspace path as a requirement even when copied through an alias. Use only capabilities supplied
by the active verification runtime. Branch membership needs authoritative refs from that workspace.
Run from the supplied current directory; do not cd to an original workspace path or inherited REPO/WORKSPACE variable.
Python probes must import this candidate's package (use an inline heredoc or explicitly prepend cwd to sys.path),
not an installed or primary-checkout copy. Verify the actual import source when the package could be ambiguous.
Compare filesystem identities after resolving both paths: a relative sys.path entry does NOT imply a relative
module.__file__. For example, compare Path(package.__file__).resolve().parent with (Path.cwd() / 'package').resolve();
never require __file__ to start with './', and do not compare unresolved '/cwd/./package' with '/cwd/package'.
Only invoke existing public test files that you actually inspected with read_file. Verify package exports and
import patterns against this frozen revision; do not invent re-exports or use knowledge of a later library version.
Prefer existing relevant public tests. When they do not cover the requested behavior, write a self-contained regression
probe as a command (for example a Python heredoc with actual assertions against the public API). Do not modify source
or test files, install dependencies, fetch external tests, or access private benchmark/grader/gold assets.
Map each check to exactly ONE existing acceptance criterion ID; cover EVERY behavior in it, not just one example.
This call targets one criterion. List its distinct obligations internally before writing checks: every named variant,
positive and negative boundary, and observable outcome must have an actual assertion. Do not promise coverage in a
rationale that the command does not implement. Do not emit placeholder checks or source-keyword searches in place of
runtime behavior. Use the supplied verification runtime capabilities for Git evidence. Check the committed candidate
and its reachable history against the supplied frozen baseline. Require a named branch or particular commit layout
only when the task specifies one. A detached verification checkout is expected. Establish earlier actions using
the relevant public records, including older reachable revisions when an entry was edited or summarized later.
For question coverage, distinguish unresolved decisions from features the user already specified. Read a full
question thread in context, including references and wrapped lines. Ground each required question in an open point.
For a reporting obligation, establish what must be reported, by whom, and under which condition. Require a
particular file, section, channel or literal wording only for the branch that explicitly requires it. Otherwise
accept equivalent wording and any contract-permitted report available to this check. Use public evidence tied to
this candidate and subject. A source showing the underlying state alone does not prove the candidate reported it.
Prefer a documented structured status when available. Avoid an arbitrary phrase list or a scan of unrelated files.
When a field requires the author's own substantive statement, identify what the author asserts about the required
subject. A complete statement may cite a placeholder, and a quotation or example alone may look complete.
Use attribution and the public contract to judge the field as a whole. Honor any explicit literal constraints.
An empty search cannot establish absence when an allowed reporting channel is outside the runtime's view.
If the available evidence cannot distinguish a valid report from missing, stale or contrary evidence, return unavailable.
When a check extracts a required value from a public record, use an existing populated record from the inspected
source as a positive control when available. For a new format, derive a minimal valid control from its public
contract or schema. If the representation is unspecified, choose another public observation that covers the
criterion. Distinguish labels and metadata from payload using the applicable format:
content may start beside a label or continue on later lines. Trace the command's exact record boundaries and
extraction on that positive control, then on a copy with only its payload removed. Preserve labels, metadata and
neighboring records in the empty control. The extractor must retain the source value on the positive control and
find no value in the empty control. These controls exercise the reader; evaluate the criterion on the candidate.
Construct a fixture that satisfies the public preconditions for each named variant. Check for documented exceptions
that would select a different behavior with the same inputs. A working control for one variant cannot establish
that a different variant's prerequisites are satisfied.
When checking agreement with a nonempty set of public records, exercise the applicable claims against candidate
behavior. Track the number of comparisons and fail on uncovered required claims, including zero comparisons.
An unrelated control, registered feature or aggregate count cannot stand in for those comparisons. If the public
requirement permits an empty input, exercise that case separately. Report unavailable when the required relation
has no observable public check.
A wrapper/type annotation must preserve the valid use of the underlying type: select a supported underlying API from
public repository evidence and compare equivalent ordinary declarations. Do not demand an unrelated feature such as
a new parameter location or new supported type. Registration alone does not prove request-time generic behavior.
For each preserved behavior, include an equivalent ordinary declaration as a control, with the same fixture and
inputs. Establish expected defaults from that control: test clients can add headers, retain cookies or transform
request data. An input omitted in the test call is not necessarily absent from the request received by the app.
Prefer explicit isolated inputs, and assert the control response before comparing the requested new declaration.
For a preservation-only criterion, compare with the ordinary control's observed result; do not invent rejection
behavior merely because an input looks unusual. Only demand a particular negative status when public evidence
establishes that boundary. A type that already infers a parameter kind does not prove its explicit marker was read;
also exercise a non-default marker option whose omission would change an asserted response or schema field.
Every fixture must obey ALL other acceptance criteria: a test of one feature cannot depend on a declaration that
another criterion explicitly forbids. Existing language/type-system equality may compare metadata by object identity;
reuse the same metadata objects when testing flattening rather than comparing separately constructed instances.
Compile literal patterns with the target Python regex syntax before using them to judge candidate data.
Trace helper calls and argument types as executable Python. Construct the valid ordinary control through the SAME
helper before an invalid declaration: an AttributeError/TypeError caused by looking up a route path as an attribute,
calling a string, or other fixture setup errors is not evidence that the API rejected the declaration. Keep fixture
setup outside rejection-catching blocks; catch only the actual API registration/validation call being tested.
Follow the repository's actual schema/documentation references before inspecting a field: an inline definition and
a referenced definition can describe the same behavior. Do not require one representation unless the user asked for it.
For source-tree implementation/provenance criteria, check the actual import location and source diff against the
provided baseline. Do not invent an internal function that must unwrap, store or return a particular representation.
For an explicit artifact-delivery criterion, execute the public submission command the user supplied from this
candidate and assert the actual requested artifact exists with the required contents. Its documented side effect is
allowed. A dirty diff, changed filename or existing stale artifact is not a substitute for executing delivery.
A command explicitly supplied by the user can be installed on the runtime PATH outside the source repository.
Its absence from the frozen source or pyproject does not prove it is unavailable. Invoke that exact command and
let a missing executable fail the runtime check; do not reject it merely because its implementation is not in Git.
If the provided delivery mechanism cannot work from a committed candidate, report unavailable rather than fabricate
a proxy criterion. Never modify the candidate Git state merely to make a dirty-diff assertion pass.
Keep checks independent: an early mission must not require a future mission to be complete. The controller runs
checks for the current mission plus all previously satisfied criteria, so regressions are also checked. The rationale
must identify the public evidence and the observable assertion, not just repeat the criterion. When a probe uses output as its verdict, read output freshly captured from that invocation, and fail
if its positive result is missing. A nonzero check exit must mean failure. Never suppress failures, return success after errors, use always-success commands, accept zero tests,
or replace behavioral checks with syntax/import-only checks. Tests may legitimately fail before the feature is fixed;
do not weaken them to make a baseline pass. Keep commands bounded and noninteractive.
Missing dependencies must fail a required check, never print SKIP and exit successfully without exercising it.
If a meaningful executable public verifier cannot be written from the available evidence, return status unavailable,
an explanation, and no checks. Do not fabricate a test path or pretend coverage. Otherwise return status ready.
Return only the closed jiuwenswarm/pact-public-verification/v1 JSON schema. These are public development checks; only the
independent benchmark verifier decides the benchmark score.
"""

PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS = (
    PUBLIC_VERIFICATION_INSTRUCTIONS
    + """

You are reviewing an untrusted draft verifier written by another side agent. Re-read the public source and tests
needed to audit its assumptions. The draft's claims and expected outputs are NOT authority. Trace every assertion to
the original requirement or established public API behavior; check fixtures against the ENTIRE Goal Contract.
Look especially for unsupported control declarations, contradictory defaults, identity-dependent comparisons,
unresolved schema references, implementation-detail assertions and checks that pass without exercising the target.
For a comparison over public records, trace the branch where relevant inputs exist but none match.
Reject success on that branch unless zero matches is itself the requested behavior.
For a record extractor, independently inspect the public example or schema used for the control and trace the
draft's extraction on both controls.
Repair a draft that drops a populated value or borrows content from a neighboring record. In the rationale identify
the public source location, the expected payload and what the submitted extractor returns for each control.
For reporting checks, trace a valid paraphrase in another permitted reporting channel when the contract allows
these alternatives. Also trace an absent report and a report about the wrong subject or opposite state.
For an authored field that permits quotation, trace the draft on a valid statement citing placeholder wording.
Remove only the author's statement, leaving the quotation or example. Also try a blank field and an otherwise
complete statement about a different subject. Require the valid control to pass and all three missing-content
controls to fail. If public evidence cannot distinguish them, return unavailable.
With an explicit destination or fixed wording, require it and
trace the otherwise-correct report in the wrong place or form. Revise unsupported restrictions or return unavailable
if the permitted evidence cannot establish the obligation.
Preserve all requested behavior and meaningful negative cases; do not weaken a check just because new behavior is
absent from the frozen baseline. Correct verifier defects only. Return a complete replacement public-verification
object for this criterion, with its reason explaining the substantive corrections or why the inspected draft is sound.
Never return an approval sentence in place of the complete executable checks.
"""
)
