import os
import subprocess
import shlex
import shutil
import time
import re
import sys
import glob as globmod
import threading
import argparse
import platform
import socket
import json


COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "white": "\033[37m",
}


def color(text: str, c: str) -> str:
  return f"{COLORS.get(c, '')}{text}{COLORS['reset']}"


def strip_quotes(s: str) -> str:
  if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
    return s[1:-1]
  return s


class BuildSystemNode:

  def __init__(self, command: str, args: str, raw_line: str, line_num: int = 0, source_file: str = ""):
    self.command = command
    self.args = args
    self.raw_line = raw_line
    self.line_num = line_num
    self.source_file = source_file

  def __repr__(self):
    return f"BuildSystemNode(command={self.command!r}, args={self.args!r})"


class BuildError(Exception):

  def __init__(self, message: str, node: BuildSystemNode = None):
    self.node = node
    if node and node.line_num:
      location = f" ({node.source_file}:{node.line_num})" if node.source_file else f" (line {node.line_num})"
    else:
      location = ""
    super().__init__(f"{message}{location}")


class BuildSystemInterpreter:

  def __init__(self, f_name: str = "main.build", dry_run: bool = False,
               verbose: bool = False, target: str = None, minimal: bool = False):
    self.f_name = f_name
    self.dry_run = dry_run
    self.verbose = verbose
    self.target = target
    self.minimal = minimal
    self.current_build = None
    self.build_nodes = []
    self.handlers = {}
    self.context = {}
    self.imports = {}
    self.env = dict(os.environ)
    self.base_dir = os.path.dirname(os.path.abspath(f_name)) or os.getcwd()
    self.step_times = []
    self._include_stack = []
    self._skip_stack = []
    self._current_file = f_name
    self._functions = {}
    self._func_recording = None
    self._func_body = []
    self._targets = {}
    self._target_recording = None
    self._target_body = []
    self._try_stack = []
    self._step_count = 0
    self._failed_steps = 0
    self._print_fn = print

    _builtins = {
        "OS":         platform.system().lower(),
        "ARCH":       platform.machine().lower(),
        "CWD":        os.getcwd(),
        "HOME":       os.path.expanduser("~"),
        "USER":       os.environ.get("USER", os.environ.get("USERNAME", "")),
        "DATE":       time.strftime("%Y-%m-%d"),
        "TIME":       time.strftime("%H:%M:%S"),
        "TIMESTAMP":  str(int(time.time())),
        "BUILD_FILE": os.path.abspath(f_name),
        "BUILD_DIR":  os.path.dirname(os.path.abspath(f_name)) or os.getcwd(),
    }
    # Auto-detect common tool versions (silent if not installed)
    _builtins.update(self._detect_tool_versions())
    self.context.update(_builtins)
    # Remember which keys are built-ins so they're hidden from the summary
    self._builtin_keys = frozenset(_builtins.keys())

    keywords = [
        "build", "run", "set", "export", "install", "from",
        "import", "echo", "copy", "move",
        "delete", "mkdir", "if", "elif", "else",
        "endif", "include", "target", "endtarget",
        "invoke", "fn", "endfn",
        "foreach", "endforeach", "parallel", "endparallel",
        "spawn", "endspawn",
        "try", "catch", "endtry", "require", "capture",
        "glob", "exit", "warn", "error", "debug", "append",
        "dotenv", "env", "section", "check", "timeout", "retry", "port",
        "npm", "cargo", "json", "toml",
    ]
    for kw in keywords:
      self.register_handler(kw, self._handle_node)

  @property
  def _skipping(self):
    return any(s["skip"] for s in self._skip_stack)

  def register_handler(self, command: str, handler):
    self.handlers[command] = handler

  def _handle_node(self, command: str, args: str, raw_line: str, line_num: int = 0):
    node = BuildSystemNode(command, args, raw_line, line_num, self._current_file)

    if self._func_recording is not None:
      if command == "endfn":
        self._functions[self._func_recording] = list(self._func_body)
        self._func_recording = None
        self._func_body = []
      else:
        self._func_body.append(node)
      return

    if self._target_recording is not None:
      if command == "endtarget":
        self._targets[self._target_recording] = list(self._target_body)
        self._target_recording = None
        self._target_body = []
      else:
        self._target_body.append(node)
      return

    if command == "fn":
      self._func_recording = args.strip()
      self._func_body = []
      return

    if command == "target":
      self._target_recording = args.strip()
      self._target_body = []
      return

    self.build_nodes.append(node)

  def interpret(self, line: str, line_num: int = 0):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
      return

    parts = stripped.split(" ", 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    handler = self.handlers.get(command)
    if handler:
      handler(command=command, args=args, raw_line=stripped, line_num=line_num)
    else:
      self._handle_node(command="__call__", args=stripped, raw_line=stripped, line_num=line_num)

  def run(self):
    self._confirm_exists()
    mode_tags = []
    if self.dry_run:
      mode_tags.append(color("[DRY RUN]", "yellow"))
    if self.target:
      mode_tags.append(color(f"[TARGET: {self.target}]", "magenta"))
    mode = " ".join(mode_tags)

    if not self.minimal:
      print(f"\n{color('=== Build System ===', 'bold')} {mode}")
      print(f"  File: {color(self.f_name, 'cyan')}")
      print(f"  Base: {color(self.base_dir, 'cyan')}\n")

    self._parse_file(self.f_name)

  def _parse_file(self, f_name: str):
    abs_path = os.path.abspath(f_name)
    if abs_path in self._include_stack:
      raise BuildError(f"Circular include detected: {f_name}")
    self._include_stack.append(abs_path)

    prev_file = self._current_file
    self._current_file = f_name

    with open(f_name, "r") as f:
      for i, line in enumerate(f, start=1):
        self.interpret(line, line_num=i)

    self._current_file = prev_file
    self._include_stack.pop()

  def _confirm_exists(self):
    if not os.path.exists(self.f_name):
      raise FileNotFoundError(f"Build file '{self.f_name}' not found.")

  def _interpolate(self, text: str) -> str:
    def _lookup(var_name: str) -> str | None:
      if var_name in self.context:
        return str(self.context[var_name])
      if var_name in self.env:
        return self.env[var_name]
      return None

    def replacer(match):
      content = match.group(1)

      # ${env:VAR} or ${env:VAR:fallback}
      if content.startswith("env:"):
        parts = content[4:].split(":", 1)
        val = os.environ.get(parts[0])
        return val if val is not None else (parts[1] if len(parts) > 1 else "")

      # ${upper:var}
      if content.startswith("upper:"):
        v = _lookup(content[6:])
        return (v or "").upper()

      # ${lower:var}
      if content.startswith("lower:"):
        v = _lookup(content[6:])
        return (v or "").lower()

      # ${trim:var}
      if content.startswith("trim:"):
        v = _lookup(content[5:])
        return (v or "").strip()

      # ${len:var}
      if content.startswith("len:"):
        v = _lookup(content[4:])
        return str(len(v or ""))

      # ${var:-default}  →  value if set and non-empty, else default
      if ":-" in content:
        var, default = content.split(":-", 1)
        v = _lookup(var)
        return v if v else default

      # ${var:+word}  →  word if var is set and non-empty, else ""
      if ":+" in content:
        var, word = content.split(":+", 1)
        v = _lookup(var)
        return word if v else ""

      # plain variable
      v = _lookup(content)
      return v if v is not None else match.group(0)

    return re.sub(r'\$\{([^}]+)\}', replacer, text)

  def _run_shell(self, command: str, cwd: str = None, node: BuildSystemNode = None, capture: bool = False):
    command = self._interpolate(command)
    work_dir = cwd or self.base_dir

    if self.verbose:
      self._print_fn(color(f"    [exec] {command} (in {work_dir})", "dim"))

    if self.dry_run:
      self._print_fn(color(f"    [dry-run] would execute: {command}", "yellow"))
      return "" if capture else None

    if capture:
      result = subprocess.run(
          command, shell=True, cwd=work_dir,
          capture_output=True, text=True,
          env={**os.environ, **self.env},
      )
      if result.returncode != 0:
        error_msg = result.stderr.strip() if result.stderr else "unknown error"
        raise BuildError(
            f"Command failed (exit {result.returncode}): {command}\n    {error_msg}", node)
      return result.stdout.strip()

    proc = subprocess.Popen(
        command, shell=True, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env={**os.environ, **self.env},
    )
    for line in proc.stdout:
      self._print_fn(f"    {line.rstrip()}")
    proc.wait()
    if proc.returncode != 0:
      raise BuildError(
          f"Command failed (exit {proc.returncode}): {command}\n    unknown error", node)
    return None

  def _log_step(self, tag: str, message: str, c: str = "green"):
    prefix = color(f"[{tag}]", c)
    self._print_fn(f"  {prefix} {message}")

  def _fork(self):
    child = BuildSystemInterpreter.__new__(BuildSystemInterpreter)
    child._targets = self._targets
    child._functions = self._functions
    child.base_dir = self.base_dir
    child.dry_run = self.dry_run
    child.verbose = self.verbose
    child.minimal = self.minimal
    child._builtin_keys = self._builtin_keys
    child.f_name = self.f_name
    child.handlers = self.handlers
    child.current_build = self.current_build
    child.target = None
    child.build_nodes = []
    child.context = dict(self.context)
    child.env = dict(self.env)
    child.imports = dict(self.imports)
    child._step_count = 0
    child._failed_steps = 0
    child._try_stack = []
    child._skip_stack = []
    child._func_body = []
    child._func_recording = None
    child._target_body = []
    child._target_recording = None
    child._include_stack = []
    child._current_file = self._current_file
    child.step_times = []
    child._print_fn = self._print_fn
    return child

  # Commands that are safe to run as global setup even when a specific target
  # is requested — everything else (invoke, run, echo, etc.) is skipped so
  # that a default top-level `invoke <target>` doesn't cause a double-run.
  _SETUP_COMMANDS = frozenset({
      "build", "set", "export", "env", "dotenv",
      "append", "import", "include",
  })

  def execute_nodes(self):
    start_time = time.time()

    if self.target:
      if self.target not in self._targets:
        raise BuildError(f"Target '{self.target}' not found. Available: {', '.join(self._targets.keys()) or 'none'}")
      if self.build_nodes:
        # Only run setup/declaration commands so a default `invoke <target>`
        # at the top level doesn't trigger a second execution.
        setup_nodes = [n for n in self.build_nodes if n.command in self._SETUP_COMMANDS]
        if setup_nodes:
          self._execute_node_list(setup_nodes)
      self._execute_node_list(self._targets[self.target])
    else:
      self._execute_node_list(self.build_nodes)

    total = time.time() - start_time
    self._print_summary(total)

  def _execute_node_list(self, nodes: list):
    executors = {
        "build": self._exec_build,
        "run": self._exec_run,
        "set": self._exec_set,
        "export": self._exec_export,
        "install": self._exec_install,
        "from": self._exec_from,
        "import": self._exec_import,
        "echo": self._exec_echo,
        "copy": self._exec_copy,
        "move": self._exec_move,
        "delete": self._exec_delete,
        "mkdir": self._exec_mkdir,
        "if": self._exec_if,
        "elif": self._exec_elif,
        "else": self._exec_else,
        "endif": self._exec_endif,
        "include": self._exec_include,
        "target": None,
        "endtarget": None,
        "invoke": self._exec_invoke,
        "fn": None,
        "endfn": None,
        "__call__": self._exec_fn_call,
        "foreach": None,
        "endforeach": None,
        "parallel": None,
        "endparallel": None,
        "spawn": None,
        "endspawn": None,
        "try": self._exec_try,
        "catch": self._exec_catch,
        "endtry": self._exec_endtry,
        "require": self._exec_require,
        "capture": self._exec_capture,
        "glob": self._exec_glob,
        "exit": self._exec_exit,
        "warn": self._exec_warn,
        "error": self._exec_error,
        "debug": self._exec_debug,
        "append": self._exec_append,
        "dotenv": self._exec_dotenv,
        "env": self._exec_env,
        "section": self._exec_section,
        "check": self._exec_check,
        "timeout": self._exec_timeout,
        "retry": self._exec_retry,
        "port": self._exec_port,
        "npm": self._exec_npm,
        "cargo": self._exec_cargo,
        "json": self._exec_json,
        "toml": self._exec_toml,
    }

    flow_commands = ("if", "elif", "else", "endif", "try", "catch", "endtry",
                     "foreach", "endforeach", "target", "endtarget",
                     "fn", "endfn")

    i = 0
    while i < len(nodes):
      node = nodes[i]

      if self._skipping and node.command not in flow_commands:
        i += 1
        continue

      if node.command == "foreach":
        body, end_idx = self._extract_block(nodes, i, "foreach", "endforeach")
        self._exec_foreach_block(node, body)
        i = end_idx + 1
        continue

      if node.command == "parallel":
        body, end_idx = self._extract_block(nodes, i, "parallel", "endparallel")
        self._exec_parallel_block(node, body)
        i = end_idx + 1
        continue

      if node.command == "spawn":
        body, end_idx = self._extract_block(nodes, i, "spawn", "endspawn")
        self._exec_spawn_block(node, body)
        i = end_idx + 1
        continue

      step_start = time.time()
      self._step_count += 1
      executor = executors.get(node.command)
      if not executor:
        i += 1
        continue

      in_try = bool(self._try_stack)
      try:
        executor(node)
      except BuildError as e:
        if in_try:
          self._try_stack[-1]["error"] = str(e)
          self._try_stack[-1]["failed"] = True
          self._skip_stack.append({"skip": True, "type": "try_skip"})
          self._failed_steps += 1
        else:
          raise

      elapsed = time.time() - step_start
      self.step_times.append((node.raw_line, elapsed))
      i += 1

  def _extract_block(self, nodes, start_idx, open_cmd, close_cmd):
    depth = 0
    body = []
    for j in range(start_idx, len(nodes)):
      if nodes[j].command == open_cmd:
        depth += 1
        if depth > 1:
          body.append(nodes[j])
      elif nodes[j].command == close_cmd:
        depth -= 1
        if depth == 0:
          return body, j
        body.append(nodes[j])
      elif j > start_idx:
        body.append(nodes[j])
    raise BuildError(f"Unclosed {open_cmd} block", nodes[start_idx])

  def _exec_build(self, node: BuildSystemNode):
    self.current_build = strip_quotes(self._interpolate(node.args))
    if not self.minimal:
      self._print_fn(f"\n{color('>>> Build Target:', 'bold')} {color(self.current_build, 'cyan')}")

  def _exec_run(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid run command: {node.args}", node)
    command = " ".join(tokens)
    self._log_step("run", command, "blue")
    self._run_shell(command, node=node)

  def _exec_set(self, node: BuildSystemNode):
    parts = node.args.split(" ", 1)
    if len(parts) != 2:
      raise BuildError(f"Invalid set command: {node.args}. Expected: set <key> <value>", node)

    key = parts[0].strip()
    value_str = parts[1].strip()

    # set VAR check CMD  —  store found path or "" (soft check, never fails)
    if value_str.startswith("check "):
      cmd_name = strip_quotes(self._interpolate(value_str[6:].strip()))
      found = shutil.which(cmd_name)
      self.context[key] = found or ""
      if found:
        detail = f"(check: {cmd_name} \u2713)"
        self._log_step("set", f"{key} = {color(found, 'green')} {color(detail, 'dim')}")
      else:
        detail = f"(check: {cmd_name} \u2717 not found)"
        self._log_step("set", f"{key} = {color('(empty)', 'dim')} {color(detail, 'red')}")
      return

    # set VAR port free N  —  store "free" or "" (soft port check, never fails)
    if value_str.startswith("port free "):
      port_str = self._interpolate(value_str[10:].strip())
      try:
        port_num = int(port_str)
        if not (1 <= port_num <= 65535):
          raise ValueError()
      except ValueError:
        raise BuildError(f"Invalid port number: {port_str!r}", node)
      free = self._check_port_free(port_num)
      self.context[key] = "free" if free else ""
      if free:
        self._log_step("set", f"{key} = {color('free', 'green')} {color(f'(port {port_num} is available)', 'dim')}")
      else:
        self._log_step("set", f"{key} = {color('(empty)', 'dim')} {color(f'(port {port_num} is in use)', 'red')}")
      return

    value = strip_quotes(self._interpolate(value_str))
    self.context[key] = value
    self._log_step("set", f"{key} = {color(value, 'cyan')}")

  def _exec_export(self, node: BuildSystemNode):
    parts = node.args.split(" ", 1)
    if len(parts) != 2:
      raise BuildError(f"Invalid export command: {node.args}. Expected: export <key> <value>", node)

    key = parts[0].strip()
    value = strip_quotes(self._interpolate(parts[1].strip()))
    self.env[key] = value
    os.environ[key] = value
    self._log_step("export", f"{key} = {color(value, 'cyan')}")

  def _exec_install(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) < 2:
      raise BuildError(f"Invalid install command: {node.args}", node)
    manager = tokens[0]
    target = self._interpolate(tokens[1])

    commands = {
        "pip": lambda: f"pip install -r {shlex.quote(target)}",
        "npm": lambda: f"npm install {shlex.quote(target)}",
        "apt": lambda: f"apt-get install -y {shlex.quote(target)}",
    }

    builder = commands.get(manager)
    if not builder:
      raise BuildError(f"Unknown package manager: {manager!r}", node)

    command = builder()
    self._log_step("install", f"{color(manager, 'magenta')}: {target}")
    self._run_shell(command, node=node)

  def _exec_from(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) < 3 or tokens[1] != "run":
      raise BuildError(f"Invalid from command: {node.args}. Expected: from <dir> run <command>", node)

    directory = self._interpolate(tokens[0])
    command = self._interpolate(" ".join(tokens[2:]))
    target_dir = os.path.join(self.base_dir, directory)

    if not os.path.isdir(target_dir):
      raise BuildError(f"Directory not found: {target_dir}", node)

    self._log_step("from", f"{color(directory, 'cyan')} -> {command}")
    self._run_shell(command, cwd=target_dir, node=node)

  def _exec_import(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) != 2:
      raise BuildError(f"Invalid import command: {node.args}", node)

    alias = tokens[0]
    source_path = self._interpolate(tokens[1])
    full_path = os.path.join(self.base_dir, source_path)

    if not os.path.exists(full_path):
      raise BuildError(f"Import source not found: {full_path}", node)

    self.imports[alias] = full_path
    self._log_step("import", f"{color(alias, 'magenta')} <- {source_path}")

  def _exec_echo(self, node: BuildSystemNode):
    message = strip_quotes(self._interpolate(node.args)).replace('\n', ' ').strip()
    self._log_step("echo", message, "cyan")

  def _exec_copy(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) != 2:
      raise BuildError(f"Invalid copy command: {node.args}", node)

    src = os.path.join(self.base_dir, self._interpolate(tokens[0]))
    dst = os.path.join(self.base_dir, self._interpolate(tokens[1]))
    self._log_step("copy", f"{tokens[0]} -> {tokens[1]}")

    if self.dry_run:
      print(color(f"    [dry-run] would copy {src} -> {dst}", "yellow"))
      return

    if os.path.isdir(src):
      if not os.path.exists(src):
        raise BuildError(f"Source directory not found: {src}", node)
      shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
      if not os.path.exists(src):
        raise BuildError(f"Source not found: {src}", node)
      dst_dir = os.path.dirname(dst)
      if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)
      shutil.copy2(src, dst)

  def _exec_move(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) != 2:
      raise BuildError(f"Invalid move command: {node.args}", node)

    src = os.path.join(self.base_dir, self._interpolate(tokens[0]))
    dst = os.path.join(self.base_dir, self._interpolate(tokens[1]))
    self._log_step("move", f"{tokens[0]} -> {tokens[1]}")

    if self.dry_run:
      print(color(f"    [dry-run] would move {src} -> {dst}", "yellow"))
      return

    if not os.path.exists(src):
      raise BuildError(f"Source not found: {src}", node)
    dst_dir = os.path.dirname(dst)
    if dst_dir:
      os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, dst)

  def _exec_delete(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid delete command: {node.args}", node)

    for target in tokens:
      pattern = os.path.join(self.base_dir, self._interpolate(target))
      matches = globmod.glob(pattern)
      if not matches:
        self._log_step("delete", target, "red")
        if self.dry_run:
          print(color(f"    [dry-run] would delete {pattern} (no matches)", "yellow"))
        continue
      for path in matches:
        rel = os.path.relpath(path, self.base_dir)
        self._log_step("delete", rel, "red")
        if self.dry_run:
          print(color(f"    [dry-run] would delete {path}", "yellow"))
          continue
        if os.path.isdir(path):
          shutil.rmtree(path)
        elif os.path.exists(path):
          os.remove(path)

  def _exec_mkdir(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid mkdir command: {node.args}", node)
    for target in tokens:
      path = os.path.join(self.base_dir, self._interpolate(target))
      self._log_step("mkdir", target)
      if self.dry_run:
        print(color(f"    [dry-run] would create {path}", "yellow"))
        continue
      os.makedirs(path, exist_ok=True)

  def _exec_if(self, node: BuildSystemNode):
    if self._skipping:
      self._skip_stack.append({"skip": True, "type": "if", "resolved": True})
      return

    tokens = shlex.split(node.args)
    result = self._evaluate_condition(tokens, node)
    if self.verbose:
      self._log_step("if", f"{node.args} -> {result}", "yellow")
    self._skip_stack.append({"skip": not result, "type": "if", "resolved": result})

  def _exec_elif(self, node: BuildSystemNode):
    if not self._skip_stack or self._skip_stack[-1]["type"] != "if":
      raise BuildError("elif without matching if", node)

    frame = self._skip_stack[-1]
    if frame["resolved"]:
      frame["skip"] = True
      return

    tokens = shlex.split(node.args)
    result = self._evaluate_condition(tokens, node)
    if self.verbose:
      self._log_step("elif", f"{node.args} -> {result}", "yellow")
    frame["skip"] = not result
    if result:
      frame["resolved"] = True

  def _exec_else(self, node: BuildSystemNode):
    if not self._skip_stack or self._skip_stack[-1]["type"] != "if":
      raise BuildError("else without matching if", node)

    frame = self._skip_stack[-1]
    if frame["resolved"]:
      frame["skip"] = True
    else:
      frame["skip"] = False
      frame["resolved"] = True

  def _exec_endif(self, node: BuildSystemNode):
    if not self._skip_stack:
      raise BuildError("endif without matching if", node)
    top = self._skip_stack[-1]
    if top["type"] != "if":
      raise BuildError("endif without matching if", node)
    self._skip_stack.pop()

  def _evaluate_condition(self, tokens: list, node: BuildSystemNode) -> bool:
    if not tokens:
      raise BuildError("Empty condition", node)

    negate = False
    if tokens[0] == "not":
      negate = True
      tokens = tokens[1:]

    if len(tokens) == 2 and tokens[0] == "exists":
      path = os.path.join(self.base_dir, self._interpolate(tokens[1]))
      result = os.path.exists(path)
    elif len(tokens) == 1:
      value = self._interpolate(tokens[0])
      if value != tokens[0]:
        # Variable was interpolated — check its value is non-empty and not a falsy literal
        result = bool(value) and value.lower() not in ("false", "0", "no", "")
      else:
        # Bare name — look up in context/env and check the *value* (not just existence)
        raw = self.context.get(tokens[0], self.env.get(tokens[0], None))
        if raw is None:
          result = False
        else:
          raw_str = str(raw)
          result = bool(raw_str) and raw_str.lower() not in ("false", "0", "no", "")
    elif len(tokens) == 3:
      left = strip_quotes(self._interpolate(tokens[0]))
      op = tokens[1]
      right = strip_quotes(self._interpolate(tokens[2]))
      ops = {
          "==": lambda: left == right,
          "!=": lambda: left != right,
          ">": lambda: left > right,
          "<": lambda: left < right,
          ">=": lambda: left >= right,
          "<=": lambda: left <= right,
          "contains": lambda: right in left,
          "startswith": lambda: left.startswith(right),
          "endswith": lambda: left.endswith(right),
          "matches": lambda: bool(re.match(right, left)),
          "semver==": lambda: self._semver_cmp(left, right) == 0,
          "semver!=": lambda: self._semver_cmp(left, right) != 0,
          "semver>":  lambda: self._semver_cmp(left, right) > 0,
          "semver>=": lambda: self._semver_cmp(left, right) >= 0,
          "semver<":  lambda: self._semver_cmp(left, right) < 0,
          "semver<=": lambda: self._semver_cmp(left, right) <= 0,
      }
      evaluator = ops.get(op)
      if not evaluator:
        raise BuildError(f"Unknown operator: {op!r}", node)
      result = evaluator()
    else:
      raise BuildError(f"Invalid condition: {' '.join(tokens)}", node)

    return not result if negate else result

  def _exec_include(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid include command: {node.args}", node)
    path = os.path.join(self.base_dir, self._interpolate(tokens[0]))
    if not os.path.exists(path):
      raise BuildError(f"Include file not found: {path}", node)
    self._log_step("include", tokens[0], "magenta")
    self._parse_file(path)

  def _exec_invoke(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid invoke command: {node.args}", node)

    target_name = self._interpolate(tokens[0])

    if target_name not in self._targets:
      raise BuildError(f"Target '{target_name}' not found. Available: {', '.join(self._targets.keys()) or 'none'}", node)

    self._log_step("invoke", f"target '{color(target_name, 'magenta')}'", "blue")
    self._execute_node_list(self._targets[target_name])

  def _exec_fn_call(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid function call: {node.args}", node)

    func_name = tokens[0]
    func_args = tokens[1:]

    if func_name not in self._functions:
      available = ', '.join(self._functions.keys()) if self._functions else 'none'
      raise BuildError(f"Unknown command or function: '{func_name}'. Defined functions: {available}", node)

    self._log_step(func_name, ", ".join(func_args) if func_args else "()", "magenta")

    old_context = dict(self.context)
    for i, arg in enumerate(func_args):
      self.context[f"arg{i}"] = self._interpolate(arg)
    self.context["argc"] = str(len(func_args))

    self._execute_node_list(self._functions[func_name])

    for key in list(self.context.keys()):
      if key.startswith("arg") or key == "argc":
        del self.context[key]
    for key, value in old_context.items():
      if key.startswith("arg") or key == "argc":
        self.context[key] = value

  def _exec_foreach_block(self, node: BuildSystemNode, body: list):
    tokens = shlex.split(node.args)
    if len(tokens) < 3 or tokens[1] != "in":
      raise BuildError(f"Invalid foreach: {node.args}. Expected: foreach <var> in <items...>", node)

    var_name = tokens[0]
    raw_items = [self._interpolate(item) for item in tokens[2:]]
    items = []
    for item in raw_items:
      if "\n" in item:
        items.extend(part for part in item.split("\n") if part)
      else:
        items.extend(item.split())

    self._log_step("foreach", f"{var_name} in [{', '.join(items)}] ({len(body)} steps x {len(items)} items)", "blue")

    old_value = self.context.get(var_name)
    for item in items:
      self.context[var_name] = item
      self._execute_node_list(body)

    if old_value is not None:
      self.context[var_name] = old_value
    elif var_name in self.context:
      del self.context[var_name]

  def _exec_parallel_block(self, node: BuildSystemNode, body: list):
    fs_commands = ("copy", "delete", "mkdir", "move", "echo")
    tasks = []
    for n in body:
      if n.command not in ("run",) + fs_commands:
        raise BuildError(
            f"Only file-system commands (run, copy, delete, mkdir, move, echo) are allowed inside parallel blocks, got: {n.command!r}", n)
      tasks.append(n)

    self._log_step("parallel", f"executing {len(tasks)} commands", "blue")

    if self.dry_run:
      for n in tasks:
        print(color(f"    [dry-run] would execute in parallel: {n.raw_line}", "yellow"))
      return

    results = [None] * len(tasks)
    errors = [None] * len(tasks)

    def run_task(idx, n):
      try:
        if n.command == "run":
          tokens = shlex.split(n.args)
          cmd = self._interpolate(" ".join(tokens))
          result = subprocess.run(
              cmd, shell=True, cwd=self.base_dir,
              capture_output=True, text=True,
              env={**os.environ, **self.env},
          )
          results[idx] = (n.raw_line, result.stdout, result.returncode)
          if result.returncode != 0:
            errors[idx] = BuildError(
                f"Parallel command failed (exit {result.returncode}): {cmd}\n    {result.stderr.strip()}", n)
        elif n.command == "echo":
          message = strip_quotes(self._interpolate(n.args)).replace('\n', ' ').strip()
          results[idx] = (n.raw_line, message, 0)
        elif n.command == "mkdir":
          for target in shlex.split(n.args):
            path = os.path.join(self.base_dir, self._interpolate(target))
            os.makedirs(path, exist_ok=True)
          results[idx] = (n.raw_line, "", 0)
        elif n.command == "delete":
          for target in shlex.split(n.args):
            pattern = os.path.join(self.base_dir, self._interpolate(target))
            for path in globmod.glob(pattern):
              if os.path.isdir(path):
                shutil.rmtree(path)
              elif os.path.exists(path):
                os.remove(path)
          results[idx] = (n.raw_line, "", 0)
        elif n.command == "copy":
          tokens = shlex.split(n.args)
          if len(tokens) != 2:
            raise BuildError(f"Invalid copy command: {n.args}", n)
          src = os.path.join(self.base_dir, self._interpolate(tokens[0]))
          dst = os.path.join(self.base_dir, self._interpolate(tokens[1]))
          if os.path.isdir(src):
            if not os.path.exists(src):
              raise BuildError(f"Source directory not found: {src}", n)
            shutil.copytree(src, dst, dirs_exist_ok=True)
          else:
            if not os.path.exists(src):
              raise BuildError(f"Source not found: {src}", n)
            dst_dir = os.path.dirname(dst)
            if dst_dir:
              os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src, dst)
          results[idx] = (n.raw_line, "", 0)
        elif n.command == "move":
          tokens = shlex.split(n.args)
          if len(tokens) != 2:
            raise BuildError(f"Invalid move command: {n.args}", n)
          src = os.path.join(self.base_dir, self._interpolate(tokens[0]))
          dst = os.path.join(self.base_dir, self._interpolate(tokens[1]))
          if not os.path.exists(src):
            raise BuildError(f"Source not found: {src}", n)
          dst_dir = os.path.dirname(dst)
          if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
          shutil.move(src, dst)
          results[idx] = (n.raw_line, "", 0)
      except BuildError as e:
        errors[idx] = e
      except Exception as e:
        errors[idx] = BuildError(f"Parallel task exception: {e}", n)

    threads = []
    for i, n in enumerate(tasks):
      t = threading.Thread(target=run_task, args=(i, n))
      threads.append(t)
      t.start()

    for t in threads:
      t.join()

    for i, n in enumerate(tasks):
      r = results[i]
      ok = r is not None and r[2] == 0 and errors[i] is None
      status = color("OK", "green") if ok else color("FAIL", "red")
      label = r[0] if r else n.raw_line
      print(f"    {status} {label}")
      if r and r[1]:
        for line in r[1].strip().split("\n"):
          print(f"      {line}")

    first_error = next((e for e in errors if e), None)
    if first_error:
      raise first_error

  def _exec_spawn_block(self, node: BuildSystemNode, body: list):
    SPAWN_COLORS = ["cyan", "yellow", "green", "magenta", "blue", "red", "white"]

    def _parse_tokens(tokens, n):
      if not tokens:
        raise BuildError("Empty command in spawn block", n)
      cmd0 = tokens[0]
      if cmd0 == "run":
        cmd = self._interpolate(" ".join(tokens[1:]))
        return None, "shell", (cmd, self.base_dir)
      if cmd0 == "from":
        if len(tokens) < 4 or tokens[2] != "run":
          raise BuildError(f"Invalid from in spawn: expected 'from <dir> run <cmd>'", n)
        directory = self._interpolate(tokens[1])
        cmd = self._interpolate(" ".join(tokens[3:]))
        cwd = os.path.join(self.base_dir, directory)
        return directory, "shell", (cmd, cwd)
      if cmd0 == "invoke":
        tname = tokens[1] if len(tokens) > 1 else ""
        if not tname or tname not in self._targets:
          avail = ", ".join(self._targets.keys()) or "none"
          raise BuildError(f"Target '{tname}' not found. Available: {avail}", n)
        return tname, "target", (tname,)
      fn_name = cmd0
      if fn_name not in self._functions:
        raise BuildError(
            f"Unknown spawn command: {fn_name!r}. Expected: run, from <dir> run, invoke <target>, or a defined function.", n)
      return fn_name, "fn", (fn_name, tokens[1:])

    def _resolve_tool_task(tool, raw_args, n):
      """Turn  npm/cargo <args> [in "<dir>"]  into a shell task tuple."""
      m = re.search(r'\s+in\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*$', raw_args)
      if m:
        directory = self._interpolate(m.group(2) or m.group(3) or m.group(4))
        cwd = os.path.join(self.base_dir, directory)
        sub = raw_args[:m.start()].strip()
      else:
        cwd = self.base_dir
        sub = raw_args.strip()
      if not sub:
        raise BuildError(f"{tool}: expected a subcommand", n)
      if not os.path.isdir(cwd):
        rel = os.path.relpath(cwd, self.base_dir)
        raise BuildError(f"{tool}: directory not found: {rel!r}", n)
      cmd = self._interpolate(f"{tool} {sub}")
      label = os.path.relpath(cwd, self.base_dir) if cwd != self.base_dir else sub.split()[0]
      return label, "shell", (cmd, cwd)

    def _make_task(n):
      if n.command in ("run", "from", "invoke"):
        tokens = [n.command] + shlex.split(n.args)
        return _parse_tokens(tokens, n)
      if n.command in ("npm", "cargo"):
        return _resolve_tool_task(n.command, n.args, n)
      if n.command == "__call__":
        m = re.match(r'^([\w][\w-]*): (.+)$', n.args)
        if m:
          explicit_label = m.group(1)
          raw_rhs = m.group(2).strip()
          rhs_tokens = shlex.split(raw_rhs)
          # Support  label: npm <args>  and  label: cargo <args>
          if rhs_tokens and rhs_tokens[0] in ("npm", "cargo"):
            tool = rhs_tokens[0]
            rhs_rest = raw_rhs[len(tool):].strip()
            _, kind, data = _resolve_tool_task(tool, rhs_rest, n)
            return explicit_label, kind, data
          _, kind, data = _parse_tokens(rhs_tokens, n)
          return explicit_label, kind, data
        return _parse_tokens(shlex.split(n.args), n)
      raise BuildError(
          f"Unsupported command in spawn block: {n.command!r}. Use run, from, invoke, npm, cargo, or a function name.", n)

    tasks = []
    for n in body:
      label, kind, data = _make_task(n)
      if label is None:
        label = f"proc-{len(tasks) + 1}"
      tasks.append((label, kind, data, n))

    if not tasks:
      return

    self._log_step("spawn", f"starting {len(tasks)} concurrent process{'es' if len(tasks) != 1 else ''}", "magenta")
    for i, (label, kind, data, _) in enumerate(tasks):
      col = SPAWN_COLORS[i % len(SPAWN_COLORS)]
      if kind == "shell":
        desc = data[0]
      elif kind == "target":
        desc = color(f"invoke {data[0]}", "dim")
      else:
        args_str = " ".join(f'"{a}"' for a in data[1]) if data[1] else ""
        desc = color(f"{data[0]} {args_str}".strip(), "dim")
      print(f"  {color(f'[{label}]', col)} {desc}")

    if self.dry_run:
      return

    lock = threading.Lock()

    def make_labeled_print(label, col):
      buf = [""]
      prefix = f"  {color(f'[{label}]', col)} "
      def fn(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args) + end
        buf[0] += text
        while "\n" in buf[0]:
          line, buf[0] = buf[0].split("\n", 1)
          stripped = line.lstrip()
          if stripped:
            with lock:
              print(f"{prefix}{stripped}", flush=True)
      return fn

    procs = []
    interp_threads = []
    thread_errors = {}

    for i, (label, kind, data, n) in enumerate(tasks):
      col = SPAWN_COLORS[i % len(SPAWN_COLORS)]

      if kind == "shell":
        cmd, cwd = data
        try:
          proc = subprocess.Popen(
              cmd, shell=True, cwd=cwd,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              text=True, bufsize=1,
              env={**os.environ, **self.env},
          )
          labeled_print = make_labeled_print(label, col)

          def _read(proc, labeled_print):
            for raw_line in proc.stdout:
              labeled_print(raw_line.rstrip())

          rt = threading.Thread(target=_read, args=(proc, labeled_print), daemon=True)
          rt.start()
          procs.append((proc, label, col, rt))
        except Exception as e:
          raise BuildError(f"Failed to spawn '{label}': {e}", n)

      else:
        child = self._fork()
        child._print_fn = make_labeled_print(label, col)

        def _run_interp(child, kind, data, label, n):
          try:
            if kind == "target":
              tname = data[0]
              child._execute_node_list(child._targets[tname])
            else:
              fn_name, fn_args = data[0], data[1]
              fake_args = " ".join(shlex.quote(a) for a in fn_args)
              fake_node = BuildSystemNode("__call__", f"{fn_name} {fake_args}".strip(),
                                         f"{fn_name} {fake_args}".strip(), n.line_num, n.source_file)
              child._exec_fn_call(fake_node)
          except BuildError as e:
            thread_errors[label] = str(e)
          except Exception as e:
            thread_errors[label] = str(e)

        t = threading.Thread(target=_run_interp, args=(child, kind, data, label, n), daemon=True)
        interp_threads.append((t, label))
        t.start()

    try:
      for proc, label, col, rt in procs:
        proc.wait()
        rt.join(timeout=2)
      for t, label in interp_threads:
        t.join()
    except KeyboardInterrupt:
      print(f"\n  {color('[spawn]', 'yellow')} interrupted — stopping all processes", flush=True)
      for proc, label, col, rt in procs:
        try:
          proc.terminate()
        except Exception:
          pass
      for proc, label, col, rt in procs:
        try:
          proc.wait(timeout=5)
        except Exception:
          try:
            proc.kill()
          except Exception:
            pass
      for t, label in interp_threads:
        t.join(timeout=3)
      raise SystemExit(130)

    failed_shell = [(lbl, proc.returncode) for proc, lbl, col, rt in procs if proc.returncode not in (0,)]
    if failed_shell:
      msgs = ", ".join(f"{lbl} (exit {code})" for lbl, code in failed_shell)
      raise BuildError(f"Spawned processes failed: {msgs}", node)
    if thread_errors:
      msgs = "; ".join(f"[{lbl}] {err}" for lbl, err in thread_errors.items())
      raise BuildError(f"Spawned tasks failed: {msgs}", node)

  def _exec_try(self, node: BuildSystemNode):
    self._try_stack.append({"error": None, "failed": False})

  def _exec_catch(self, node: BuildSystemNode):
    if not self._try_stack:
      raise BuildError("catch without matching try", node)

    frame = self._try_stack[-1]

    while self._skip_stack and self._skip_stack[-1].get("type") == "try_skip":
      self._skip_stack.pop()

    if frame["failed"]:
      self._log_step("catch", f"handling error: {frame['error']}", "yellow")
      self.context["_error"] = frame["error"]
    else:
      self._skip_stack.append({"skip": True, "type": "catch_skip"})

  def _exec_endtry(self, node: BuildSystemNode):
    if not self._try_stack:
      raise BuildError("endtry without matching try", node)

    while self._skip_stack and self._skip_stack[-1].get("type") in ("try_skip", "catch_skip"):
      self._skip_stack.pop()

    self._try_stack.pop()
    if "_error" in self.context:
      del self.context["_error"]

  def _exec_require(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    msg_idx = None
    for i, t in enumerate(tokens):
      if t == "||":
        msg_idx = i
        break

    if msg_idx is not None:
      condition_tokens = tokens[:msg_idx]
      message = " ".join(tokens[msg_idx + 1:])
    else:
      condition_tokens = tokens
      message = f"Requirement not met: {node.args}"

    result = self._evaluate_condition(condition_tokens, node)
    if not result:
      raise BuildError(self._interpolate(message), node)

    if self.verbose:
      self._log_step("require", f"{node.args} -> {color('OK', 'green')}", "green")

  def _exec_capture(self, node: BuildSystemNode):
    parts = node.args.split(" ", 1)
    if len(parts) != 2:
      raise BuildError(f"Invalid capture command: {node.args}. Expected: capture <var> <command>", node)

    var_name = parts[0].strip()
    command = strip_quotes(parts[1].strip())

    self._log_step("capture", f"{var_name} <- `{command}`", "blue")
    output = self._run_shell(command, node=node, capture=True)
    self.context[var_name] = output or ""

  def _exec_glob(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if len(tokens) != 2:
      raise BuildError(f"Invalid glob command: {node.args}. Expected: glob <var> <pattern>", node)

    var_name = tokens[0]
    pattern = self._interpolate(tokens[1])
    full_pattern = os.path.join(self.base_dir, pattern)
    matches = sorted(globmod.glob(full_pattern))
    relative = [os.path.relpath(m, self.base_dir) for m in matches]
    self.context[var_name] = "\n".join(relative) + "\n" if relative else ""
    self._log_step("glob", f"{var_name} = [{', '.join(relative)}] ({len(relative)} matches)", "blue")

  def _exec_append(self, node: BuildSystemNode):
    parts = node.args.split(" ", 1)
    if len(parts) != 2:
      raise BuildError(f"Invalid append command: {node.args}. Expected: append <var> <value>", node)
    key = parts[0].strip()
    value = strip_quotes(self._interpolate(parts[1].strip()))
    existing = self.context.get(key, "")
    self.context[key] = f"{existing} {value}".strip() if existing else value
    self._log_step("append", f"{key} += {color(value, 'cyan')}")

  def _exec_dotenv(self, node: BuildSystemNode):
    parts = node.args.split()
    if not parts:
      raise BuildError("dotenv requires a file path", node)
    optional = "optional" in parts
    file_arg = next((p for p in parts if p != "optional"), None)
    file_path = strip_quotes(self._interpolate(file_arg))
    full_path = os.path.join(self.base_dir, file_path)

    if not os.path.exists(full_path):
      if optional:
        self._log_step("dotenv", f"{color(file_path, 'cyan')} {color('(not found, skipped)', 'dim')}")
        return
      raise BuildError(f"dotenv file not found: {file_path}", node)

    loaded = 0
    with open(full_path, "r") as f:
      for raw_line in f:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
          continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
          value = value[1:-1]
        self.context[key] = value
        self.env[key] = value
        os.environ[key] = value
        loaded += 1

    self._log_step("dotenv", f"{color(file_path, 'cyan')} — {loaded} variable{'s' if loaded != 1 else ''} loaded")

  def _exec_env(self, node: BuildSystemNode):
    parts = node.args.split(None, 1)
    if not parts:
      raise BuildError("env requires a variable name", node)
    var_name = parts[0].strip()
    default = strip_quotes(self._interpolate(parts[1].strip())) if len(parts) > 1 else ""
    value = os.environ.get(var_name)
    if value is not None:
      self.context[var_name] = value
      self._log_step("env", f"{color(var_name, 'cyan')} = {color(value, 'green')} {color('(from environment)', 'dim')}")
    else:
      self.context[var_name] = default
      self._log_step("env", f"{color(var_name, 'cyan')} = {color(default, 'yellow')} {color('(default)', 'dim')}")

  def _exec_section(self, node: BuildSystemNode):
    title = strip_quotes(self._interpolate(node.args)).strip()
    bar_total = 54
    side = max(2, (bar_total - len(title) - 2) // 2)
    bar = "─" * side
    banner = f"{bar} {title} {bar}"
    self._print_fn(f"\n  {color(banner, 'bold')}\n")

  def _exec_check(self, node: BuildSystemNode):
    args = node.args

    # check cmd → varname  (or ->)  — soft mode: store result, never fail
    store_var = None
    for sep in ("→", "->"):
      if sep in args:
        args, _, store_var = args.partition(sep)
        store_var = store_var.strip()
        args = args.strip()
        break

    parts = args.split("||", 1)
    cmd_name = strip_quotes(self._interpolate(parts[0].strip()))
    err_msg = strip_quotes(self._interpolate(parts[1].strip())) if len(parts) > 1 else None

    found = shutil.which(cmd_name)

    if store_var is not None:
      self.context[store_var] = found or ""
      if found:
        detail = "(" + found + ")"
        self._log_step("check", color(cmd_name, "cyan") + " " + color("✓", "green") + " " + color(detail, "dim") + " → " + color(store_var, "yellow"))
      else:
        self._log_step("check", color(cmd_name, "cyan") + " " + color("✗ not found", "red") + " → " + color(store_var, "yellow") + " = (empty)")
      return

    if found:
      self._log_step("check", f"{color(cmd_name, 'cyan')} {color('✓', 'green')} {color(f'({found})', 'dim')}")
    else:
      msg = err_msg if err_msg else f"'{cmd_name}' is not installed or not found on PATH"
      raise BuildError(msg, node)

  def _check_port_free(self, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      s.settimeout(0.3)
      try:
        s.connect(("127.0.0.1", port))
        return False  # connected → something is listening → port is in use
      except (ConnectionRefusedError, OSError):
        return True   # refused → nothing listening → port is free

  def _exec_port(self, node: BuildSystemNode):
    args = node.args

    # port free N → varname  (or ->)  — soft mode
    store_var = None
    for sep in ("→", "->"):
      if sep in args:
        args, _, store_var = args.partition(sep)
        store_var = store_var.strip()
        args = args.strip()
        break

    # split off any || message
    parts = args.split("||", 1)
    main_args = parts[0].strip()
    err_msg = strip_quotes(self._interpolate(parts[1].strip())) if len(parts) > 1 else None

    tokens = main_args.split()
    if not tokens or tokens[0] != "free":
      raise BuildError("port: expected 'port free <number>'", node)
    if len(tokens) < 2:
      raise BuildError("port free: expected a port number", node)

    port_str = self._interpolate(tokens[1])
    try:
      port_num = int(port_str)
      if not (1 <= port_num <= 65535):
        raise ValueError()
    except ValueError:
      raise BuildError(f"Invalid port number: {port_str!r} (must be 1–65535)", node)

    free = self._check_port_free(port_num)

    if store_var is not None:
      self.context[store_var] = "free" if free else ""
      if free:
        self._log_step("port", color(f"{port_num}", "cyan") + " " + color("✓ free", "green") + " → " + color(store_var, "yellow"))
      else:
        self._log_step("port", color(f"{port_num}", "cyan") + " " + color("✗ in use", "red") + " → " + color(store_var, "yellow") + " = (empty)")
      return

    if free:
      self._log_step("port", f"{color(str(port_num), 'cyan')} {color('✓ free', 'green')}")
    else:
      msg = err_msg if err_msg else f"Port {port_num} is already in use"
      raise BuildError(msg, node)

  # ── semver helper ──────────────────────────────────────────────────────────

  def _semver_cmp(self, a: str, b: str) -> int:
    """Return -1, 0, or 1 comparing two semver strings (ignores pre-release labels)."""
    def parse(v: str):
      v = v.strip().lstrip("vV").split("-")[0].split("+")[0]
      parts = v.split(".")
      result = []
      for p in parts[:3]:
        try:
          result.append(int(p))
        except ValueError:
          result.append(0)
      while len(result) < 3:
        result.append(0)
      return tuple(result)
    try:
      ta, tb = parse(a), parse(b)
      if ta < tb: return -1
      if ta > tb: return 1
      return 0
    except Exception:
      return 0

  @staticmethod
  def _detect_tool_versions() -> dict:
    """Probe installed tools and return their versions as context variables.
    All probes are silent — a missing tool simply produces an empty string."""
    def _ver(cmd: str, pat: str = r"(\d+\.\d+[\.\d]*)") -> str:
      try:
        out = subprocess.check_output(
          cmd, shell=True, stderr=subprocess.STDOUT, timeout=3,
          text=True, env={**os.environ, "NO_COLOR": "1"},
        )
        m = re.search(pat, out)
        return m.group(1) if m else ""
      except Exception:
        return ""

    versions: dict = {}
    # Node / npm / pnpm / yarn / bun
    versions["NODE_VERSION"]  = _ver("node --version", r"v?(\d+\.\d+[\.\d]*)")
    versions["NPM_VERSION"]   = _ver("npm --version")
    versions["PNPM_VERSION"]  = _ver("pnpm --version")
    versions["YARN_VERSION"]  = _ver("yarn --version")
    versions["BUN_VERSION"]   = _ver("bun --version", r"bun v?(\d+\.\d+[\.\d]*)")
    # Rust
    versions["RUSTC_VERSION"] = _ver("rustc --version", r"rustc (\d+\.\d+[\.\d]*)")
    versions["CARGO_VERSION"] = _ver("cargo --version", r"cargo (\d+\.\d+[\.\d]*)")
    # Python / Go / Deno
    versions["PYTHON_VERSION"] = _ver("python3 --version", r"Python (\d+\.\d+[\.\d]*)")
    versions["GO_VERSION"]     = _ver("go version",  r"go(\d+\.\d+[\.\d]*)")
    versions["DENO_VERSION"]   = _ver("deno --version", r"deno (\d+\.\d+[\.\d]*)")
    return {k: v for k, v in versions.items()}  # include empties so `if not NODE_VERSION` works

  # ── npm ────────────────────────────────────────────────────────────────────

  def _exec_npm(self, node: BuildSystemNode):
    self._exec_tool("npm", node)

  def _exec_cargo(self, node: BuildSystemNode):
    self._exec_tool("cargo", node)

  def _exec_tool(self, tool: str, node: BuildSystemNode):
    """Generic wrapper for npm/cargo with optional 'in <dir>' suffix."""
    raw = node.args.strip()
    cwd = self.base_dir

    # Strip trailing:  in "dir"  or  in 'dir'  or  in dir
    m = re.search(r'\s+in\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*$', raw)
    if m:
      directory = self._interpolate(m.group(2) or m.group(3) or m.group(4))
      cwd = os.path.join(self.base_dir, directory)
      raw = raw[:m.start()].strip()

    if not raw:
      raise BuildError(f"{tool}: expected a subcommand (build, test, install, ...)", node)

    if not os.path.isdir(cwd):
      rel_cwd = os.path.relpath(cwd, self.base_dir)
      raise BuildError(f"{tool}: directory not found: {rel_cwd!r}", node)

    cmd = f"{tool} {raw}"
    rel = os.path.relpath(cwd, self.base_dir)
    loc = f"  {color(f'({rel})', 'dim')}" if cwd != self.base_dir and rel not in (".", "") else ""
    self._log_step(tool, f"{color(raw, 'cyan')}{loc}")
    self._run_shell(cmd, cwd=cwd, node=node)

  # ── json ───────────────────────────────────────────────────────────────────

  def _exec_json(self, node: BuildSystemNode):
    """json <key.path> from "<file>" [as <varname>]"""
    raw = node.args.strip()
    var_name = None

    m = re.match(r'^(.*?)\s+as\s+(\w+)\s*$', raw)
    if m:
      raw = m.group(1).strip()
      var_name = m.group(2)

    m2 = re.match(r'^(\S+)\s+from\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*$', raw)
    if not m2:
      raise BuildError('json: expected: json <key> from "<file>" [as <varname>]', node)

    key_path = m2.group(1)
    file_str = self._interpolate(m2.group(3) or m2.group(4) or m2.group(5))
    if var_name is None:
      var_name = key_path.split(".")[-1]

    full_path = os.path.join(self.base_dir, file_str)
    if not os.path.exists(full_path):
      raise BuildError(f"json: file not found: {file_str}", node)

    with open(full_path, encoding="utf-8") as f:
      data = json.load(f)

    value = data
    for part in key_path.split("."):
      if isinstance(value, dict) and part in value:
        value = value[part]
      else:
        raise BuildError(f"json: key '{key_path}' not found in {file_str}", node)

    self.context[var_name] = str(value)
    self._log_step("json", (
      f"{color(key_path, 'dim')} {color('from', 'dim')} {file_str}"
      f" {color('→', 'dim')} {color(var_name, 'yellow')} = {color(str(value), 'cyan')}"
    ))

  # ── toml ───────────────────────────────────────────────────────────────────

  def _exec_toml(self, node: BuildSystemNode):
    """toml <key> from "<file>" [as <varname>]
    key may be bare ("version" → looks in [package]) or "section.key".
    """
    raw = node.args.strip()
    var_name = None

    m = re.match(r'^(.*?)\s+as\s+(\w+)\s*$', raw)
    if m:
      raw = m.group(1).strip()
      var_name = m.group(2)

    m2 = re.match(r'^(\S+)\s+from\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*$', raw)
    if not m2:
      raise BuildError('toml: expected: toml <key> from "<file>" [as <varname>]', node)

    key_path = m2.group(1)
    file_str = self._interpolate(m2.group(3) or m2.group(4) or m2.group(5))
    if var_name is None:
      var_name = key_path.split(".")[-1]

    full_path = os.path.join(self.base_dir, file_str)
    if not os.path.exists(full_path):
      raise BuildError(f"toml: file not found: {file_str}", node)

    with open(full_path, encoding="utf-8") as f:
      content = f.read()

    parts = key_path.split(".", 1)
    if len(parts) == 2:
      section, key = parts
    else:
      section, key = "package", parts[0]

    value = self._toml_get(content, section, key, node, file_str)
    self.context[var_name] = value
    self._log_step("toml", (
      f"{color(key_path, 'dim')} {color('from', 'dim')} {file_str}"
      f" {color('→', 'dim')} {color(var_name, 'yellow')} = {color(value, 'cyan')}"
    ))

  def _toml_get(self, content: str, section: str, key: str,
                node: BuildSystemNode, file_str: str) -> str:
    """Extract a scalar string value from a named [section] in TOML content."""
    sec_pat = re.compile(r'^\[' + re.escape(section) + r'\]', re.MULTILINE)
    m = sec_pat.search(content)
    if not m:
      raise BuildError(f"toml: section '[{section}]' not found in {file_str}", node)
    chunk = content[m.end():]
    # Stop at next section header
    nxt = re.search(r'^\[', chunk, re.MULTILINE)
    if nxt:
      chunk = chunk[:nxt.start()]
    # Match:  key = "value"  or  key = 'value'  or  key = value
    kp = re.compile(
      r'^\s*' + re.escape(key) + r'\s*=\s*(?:"([^"]*?)"|\'([^\']*?)\'|([^\s#\n][^\n]*?))\s*(?:#[^\n]*)?\s*$',
      re.MULTILINE,
    )
    km = kp.search(chunk)
    if not km:
      raise BuildError(f"toml: key '{key}' not found in [{section}] in {file_str}", node)
    return (km.group(1) or km.group(2) or km.group(3) or "").strip()

  def _parse_run_tail(self, tail: str, node: BuildSystemNode):
    """Parse 'run <cmd>' or 'from <dir> run <cmd>', return (raw_cmd, cwd)."""
    tokens = shlex.split(tail)
    if not tokens:
      raise BuildError("Expected 'run <cmd>' or 'from <dir> run <cmd>'", node)
    if tokens[0] == "run":
      return " ".join(tokens[1:]), self.base_dir
    if tokens[0] == "from" and len(tokens) >= 4 and tokens[2] == "run":
      directory = self._interpolate(tokens[1])
      return " ".join(tokens[3:]), os.path.join(self.base_dir, directory)
    raise BuildError(f"Expected 'run <cmd>' or 'from <dir> run <cmd>', got: {tail!r}", node)

  def _exec_timeout(self, node: BuildSystemNode):
    parts = node.args.split(None, 1)
    if len(parts) < 2:
      raise BuildError("timeout: expected 'timeout <seconds> run <cmd>'", node)
    try:
      seconds = float(parts[0])
    except ValueError:
      raise BuildError(f"Invalid timeout value: {parts[0]!r}", node)

    raw_cmd, cwd = self._parse_run_tail(parts[1].strip(), node)
    cmd = self._interpolate(raw_cmd)
    self._log_step("timeout", f"{color(f'{seconds}s', 'yellow')} → {cmd}")

    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env={**os.environ, **self.env},
    )
    reader = threading.Thread(
        target=lambda: [self._print_fn(f"    {l.rstrip()}") for l in proc.stdout],
        daemon=True,
    )
    reader.start()
    try:
      proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
      proc.kill()
      proc.wait()
      reader.join(timeout=2)
      raise BuildError(f"Timed out after {seconds}s: {cmd}", node)
    reader.join(timeout=2)
    if proc.returncode != 0:
      raise BuildError(f"Command failed (exit {proc.returncode}): {cmd}", node)

  def _exec_retry(self, node: BuildSystemNode):
    parts = node.args.split(None, 1)
    if len(parts) < 2:
      raise BuildError("retry: expected 'retry <n> run <cmd>'", node)
    try:
      max_attempts = int(parts[0])
      if max_attempts < 1:
        raise ValueError()
    except ValueError:
      raise BuildError(f"Invalid retry count: {parts[0]!r} (must be a positive integer)", node)

    raw_cmd, cwd = self._parse_run_tail(parts[1].strip(), node)
    cmd = self._interpolate(raw_cmd)
    self._log_step("retry", f"up to {max_attempts}× → {cmd}")

    last_error = None
    for attempt in range(1, max_attempts + 1):
      try:
        if attempt > 1:
          delay = min(attempt * 0.5, 3.0)
          self._print_fn(color(f"    ↻ attempt {attempt}/{max_attempts} (waiting {delay:.1f}s...)", "yellow"))
          time.sleep(delay)
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, **self.env},
        )
        for line in proc.stdout:
          self._print_fn(f"    {line.rstrip()}")
        proc.wait()
        if proc.returncode != 0:
          raise BuildError(f"exit {proc.returncode}", node)
        return
      except BuildError as e:
        last_error = e
        if attempt < max_attempts:
          self._print_fn(color(f"    ✗ failed, retrying...", "yellow"))
    raise BuildError(
        f"Command failed after {max_attempts} attempt{'s' if max_attempts > 1 else ''}: {cmd}",
        node,
    )

  def _exec_exit(self, node: BuildSystemNode):
    code_str = self._interpolate(node.args).strip() if node.args else "0"
    try:
      code = int(code_str)
    except ValueError:
      code = 1
    if code != 0:
      raise BuildError(f"Build exited with code {code}", node)
    self._log_step("exit", "build finished successfully", "green")
    raise SystemExit(0)

  def _exec_warn(self, node: BuildSystemNode):
    message = strip_quotes(self._interpolate(node.args)).replace('\n', ' ').strip()
    self._log_step("warn", message, "yellow")

  def _exec_error(self, node: BuildSystemNode):
    message = strip_quotes(self._interpolate(node.args)).replace('\n', ' ').strip()
    self._log_step("error", message, "red")
    raise BuildError(message, node)

  def _exec_debug(self, node: BuildSystemNode):
    if self.verbose:
      message = strip_quotes(self._interpolate(node.args)).replace('\n', ' ').strip()
      self._log_step("debug", message, "dim")

  def _print_summary(self, total_time: float):
    if self.minimal:
      status = color("done", "green") if not self._failed_steps else color("partial", "yellow")
      print(f"\n{status} in {color(f'{total_time:.3f}s', 'green')}")
      return

    print(f"\n{color('=== Build Summary ===', 'bold')}")
    print(f"  Target:    {color(self.target or self.current_build or 'none', 'cyan')}")
    print(f"  Steps:     {self._step_count}")
    if self._failed_steps:
      print(f"  Failed:    {color(str(self._failed_steps), 'red')}")
    print(f"  Duration:  {color(f'{total_time:.3f}s', 'green')}")
    print(f"  Status:    {color('COMPLETE', 'green') if not self._failed_steps else color('PARTIAL', 'yellow')}")

    user_context = {k: v for k, v in self.context.items()
                    if k not in self._builtin_keys}
    if user_context:
      print(f"\n  {color('Context:', 'bold')}")
      for key, value in user_context.items():
        display_val = str(value).replace('\n', ' ').strip()
        print(f"    {key} = {color(display_val, 'cyan')}")

    if self.imports:
      print(f"\n  {color('Imports:', 'bold')}")
      for alias, path in self.imports.items():
        print(f"    {color(alias, 'magenta')} <- {path}")

    if self._targets:
      print(f"\n  {color('Targets:', 'bold')}")
      for name, body in self._targets.items():
        print(f"    {color(name, 'magenta')} ({len(body)} steps)")

    if self._functions:
      print(f"\n  {color('Functions:', 'bold')}")
      for name, body in self._functions.items():
        print(f"    {color(name, 'magenta')} ({len(body)} steps)")

    if self.dry_run:
      print(f"\n  {color('(No changes were made — dry run)', 'yellow')}")

    slow_steps = [(line, elapsed) for line, elapsed in self.step_times if elapsed >= 1.0]
    if slow_steps:
      print(f"\n  {color('Slow Steps (>= 1s):', 'bold')}")
      for line, elapsed in slow_steps:
        c = "yellow" if elapsed < 2 else "red"
        bar = color(f"{elapsed:.3f}s", c)
        print(f"    {bar}  {line}")

    if self.verbose and self.step_times:
      print(f"\n  {color('Step Timings:', 'bold')}")
      for line, elapsed in self.step_times:
        c = "green" if elapsed < 0.5 else "yellow" if elapsed < 2 else "red"
        bar = color(f"{elapsed:.3f}s", c)
        print(f"    {bar}  {line}")

    print()


def _resolve_positional(args):
  build_file = "main.build"
  target = None
  files = []
  targets = []

  for arg in args:
    if arg.endswith(".build") or "/" in arg or os.path.sep in arg:
      files.append(arg)
    else:
      targets.append(arg)

  if len(files) > 1:
    print(f"{color('Error:', 'red')} Multiple build files specified: {', '.join(files)}")
    sys.exit(1)
  if len(targets) > 1:
    print(f"{color('Error:', 'red')} Multiple targets specified: {', '.join(targets)}")
    print(f"  Use separate runs for each target, or combine them into one target with 'invoke'.")
    sys.exit(1)

  if files:
    build_file = files[0]
  if targets:
    target = targets[0]

  return build_file, target


def _render_plan_nodes(nodes, indent=4, target_bodies=None, fn_bodies=None):
  prefix = " " * indent
  block_commands = {"if", "foreach", "parallel", "try", "fn", "target"}
  close_commands = {"endif", "endforeach", "endparallel", "endtry", "endfn", "endtarget"}

  i = 0
  while i < len(nodes):
    node = nodes[i]
    cmd = node.command
    if cmd == "__call__":
      parts = node.args.split(None, 1)
      cmd_display = parts[0]
      args_display = parts[1] if len(parts) > 1 else ""
    else:
      cmd_display = cmd
      args_display = node.args

    if cmd in close_commands:
      i += 1
      continue

    if cmd in ("foreach", "parallel", "try"):
      open_kw = cmd
      close_kw = {"foreach": "endforeach", "parallel": "endparallel", "try": "endtry"}[cmd]
      branch_kws = {"try": ("catch",)}.get(cmd, ())
      print(f"{prefix}{color(cmd_display, 'blue')} {args_display}".rstrip())
      depth = 0
      j = i
      segment = []
      while j < len(nodes):
        nc = nodes[j]
        if nc.command == open_kw:
          depth += 1
          if depth > 1:
            segment.append(nc)
        elif nc.command == close_kw:
          depth -= 1
          if depth == 0:
            if segment:
              _render_plan_nodes(segment, indent + 2, target_bodies, fn_bodies)
            break
          segment.append(nc)
        elif depth == 1 and nc.command in branch_kws:
          if segment:
            _render_plan_nodes(segment, indent + 2, target_bodies, fn_bodies)
          segment = []
          print(f"{prefix}{color(nc.command, 'blue')} {nc.args}".rstrip())
        elif j > i:
          segment.append(nc)
        j += 1
      print(f"{prefix}{color(close_kw, 'blue')}")
      i = j + 1
      continue

    if cmd == "if":
      print(f"{prefix}{color('if', 'blue')} {args_display}")
      depth = 0
      j = i
      segment = []
      while j < len(nodes):
        nc = nodes[j]
        if nc.command == "if":
          depth += 1
          if depth > 1:
            segment.append(nc)
        elif nc.command == "endif":
          depth -= 1
          if depth == 0:
            if segment:
              _render_plan_nodes(segment, indent + 2, target_bodies, fn_bodies)
            break
          segment.append(nc)
        elif depth == 1 and nc.command in ("elif", "else"):
          if segment:
            _render_plan_nodes(segment, indent + 2, target_bodies, fn_bodies)
          segment = []
          nc_args = nc.args
          print(f"{prefix}{color(nc.command, 'blue')} {nc_args}".rstrip())
        elif j > i:
          segment.append(nc)
        j += 1
      print(f"{prefix}{color('endif', 'blue')}")
      i = j + 1
      continue

    if cmd in ("elif", "else", "catch"):
      print(f"{prefix}{color(cmd_display, 'blue')} {args_display}".rstrip())
      i += 1
      continue

    print(f"{prefix}{color(cmd_display, 'blue')} {args_display}".rstrip())
    i += 1


def main():
  parser = argparse.ArgumentParser(
      prog="builder",
      description="Builder — Modern Build Runner",
      formatter_class=argparse.RawDescriptionHelpFormatter,
      epilog=f"""
{color('examples:', 'bold')}
  builder                       Run main.build
  builder deploy                Run the 'deploy' target
  builder app.build             Run a specific build file
  builder app.build deploy      Run 'deploy' target from app.build
  builder test --verbose        Run 'test' target with verbose output
  builder clean --dry-run       Preview the 'clean' target
  builder app.build --minimal   Run with clean step-only output
  builder --list                List available targets and functions
  builder --plan                Show the execution plan without running
  builder --watch               Re-run build on .build file changes

{color('keywords:', 'dim')}
  build, set, export, echo, run, capture, install, from, import,
  copy, move, delete, mkdir, glob, append, if/elif/else/endif,
  foreach/endforeach, fn/endfn, target/endtarget, invoke,
  parallel/endparallel, try/catch/endtry, require, include,
  exit, warn, error, debug
      """,
  )
  parser.add_argument("positional", nargs="*", help="Build file (.build) and/or target name")
  parser.add_argument("--dry-run", action="store_true", help="Show steps without executing")
  parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
  parser.add_argument("-m", "--minimal", action="store_true", help="Clean output — steps only, no header or context summary")
  parser.add_argument("-t", "--target", type=str, default=None, help="Run a specific target")
  parser.add_argument("--list", action="store_true", help="List available targets and functions")
  parser.add_argument("--plan", action="store_true", help="Show execution plan without running")
  parser.add_argument("--watch", action="store_true", help="Re-run build when .build files change")

  args = parser.parse_args()

  build_file, positional_target = _resolve_positional(args.positional)
  target = args.target or positional_target

  def run_once():
    interpreter = BuildSystemInterpreter(
        f_name=build_file,
        dry_run=args.dry_run or args.plan,
        verbose=args.verbose,
        target=target,
        minimal=args.minimal,
    )

    try:
      interpreter.run()

      if args.list:
        has_content = False
        if interpreter._targets:
          print(f"\n{color('Targets:', 'bold')}")
          for name, body in interpreter._targets.items():
            print(f"  {color(name, 'magenta')}  ({len(body)} steps)")
          has_content = True
        if interpreter._functions:
          print(f"\n{color('Functions:', 'bold')}")
          for name, body in interpreter._functions.items():
            print(f"  {color(name, 'magenta')}  ({len(body)} steps)")
          has_content = True
        if not has_content:
          print("No targets or functions defined.")
        print()
        return True

      if args.plan:
        print(f"\n{color('Execution Plan:', 'bold')}")
        print(f"  File:   {color(build_file, 'cyan')}")
        if target:
          print(f"  Target: {color(target, 'magenta')}")
        print(f"  Steps:  {len(interpreter.build_nodes)} top-level nodes")
        if target and target not in interpreter._targets:
          available = ', '.join(interpreter._targets.keys()) if interpreter._targets else 'none'
          print(f"\n{color('Error:', 'red')} Target '{target}' not found. Available: {available}")
          sys.exit(1)

        if interpreter._functions:
          print(f"\n  {color('Functions:', 'bold')}")
          for name, body in interpreter._functions.items():
            print(f"    {color('fn', 'blue')} {color(name, 'magenta')}  ({len(body)} steps)")
            _render_plan_nodes(body, indent=6,
                               target_bodies=interpreter._targets,
                               fn_bodies=interpreter._functions)
            print(f"    {color('endfn', 'blue')}")

        if interpreter._targets:
          print(f"\n  {color('Targets:', 'bold')}")
          for name, body in interpreter._targets.items():
            marker = color(" <--", "green") if name == target else ""
            print(f"    {color('target', 'blue')} {color(name, 'magenta')}  ({len(body)} steps){marker}")
            _render_plan_nodes(body, indent=6,
                               target_bodies=interpreter._targets,
                               fn_bodies=interpreter._functions)
            print(f"    {color('endtarget', 'blue')}")

        print(f"\n  {color('Steps:', 'bold')}")
        nodes = interpreter._targets[target] if target else interpreter.build_nodes
        _render_plan_nodes(nodes, indent=4,
                           target_bodies=interpreter._targets,
                           fn_bodies=interpreter._functions)
        print()
        return True

      interpreter.execute_nodes()
      return True
    except SystemExit as e:
      sys.exit(e.code)
    except BuildError as e:
      print(f"\n{color('BUILD FAILED:', 'red')} {e}")
      return False
    except KeyboardInterrupt:
      raise

  if args.watch:
    base_dir = os.path.dirname(os.path.abspath(build_file)) or os.getcwd()

    def get_build_mtimes():
      mtimes = {}
      for path in globmod.glob(os.path.join(base_dir, "**/*.build"), recursive=True):
        try:
          mtimes[path] = os.path.getmtime(path)
        except OSError:
          pass
      return mtimes

    try:
      run_once()
      mtimes = get_build_mtimes()
      print(f"\n{color('--- watching .build files for changes (Ctrl+C to stop) ---', 'dim')}")
      while True:
        time.sleep(1)
        new_mtimes = get_build_mtimes()
        changed = (set(new_mtimes) != set(mtimes) or
                   any(new_mtimes.get(p) != mtimes.get(p) for p in new_mtimes))
        if changed:
          print(f"\n{color('=== Change detected — re-running build ===', 'bold')}")
          run_once()
          mtimes = new_mtimes
          print(f"\n{color('--- watching .build files for changes (Ctrl+C to stop) ---', 'dim')}")
    except KeyboardInterrupt:
      print(f"\n{color('BUILD INTERRUPTED', 'yellow')}")
      sys.exit(130)
  else:
    try:
      success = run_once()
      if not success:
        sys.exit(1)
    except KeyboardInterrupt:
      print(f"\n{color('BUILD INTERRUPTED', 'yellow')}")
      sys.exit(130)


if __name__ == "__main__":
  main()
