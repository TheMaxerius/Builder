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
               verbose: bool = False, target: str = None):
    self.f_name = f_name
    self.dry_run = dry_run
    self.verbose = verbose
    self.target = target
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

    keywords = [
        "build", "run", "set", "export", "install", "from",
        "import", "echo", "copy", "move",
        "delete", "mkdir", "if", "elif", "else",
        "endif", "include", "target", "endtarget",
        "invoke", "fn", "endfn",
        "foreach", "endforeach", "parallel", "endparallel",
        "try", "catch", "endtry", "require", "capture",
        "glob", "exit", "warn", "error", "debug",
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
    def replacer(match):
      var_name = match.group(1)
      if var_name in self.context:
        return str(self.context[var_name])
      if var_name in self.env:
        return self.env[var_name]
      return match.group(0)

    return re.sub(r'\$\{(\w+)\}', replacer, text)

  def _run_shell(self, command: str, cwd: str = None, node: BuildSystemNode = None, capture: bool = False):
    command = self._interpolate(command)
    work_dir = cwd or self.base_dir

    if self.verbose:
      print(color(f"    [exec] {command} (in {work_dir})", "dim"))

    if self.dry_run:
      print(color(f"    [dry-run] would execute: {command}", "yellow"))
      return "" if capture else None

    result = subprocess.run(
        command,
        shell=True,
        cwd=work_dir,
        capture_output=True,
        text=True,
        env={**os.environ, **self.env},
    )
    if not capture and result.stdout:
      for line in result.stdout.rstrip().split("\n"):
        print(f"    {line}")
    if result.returncode != 0:
      error_msg = result.stderr.strip() if result.stderr else "unknown error"
      raise BuildError(
          f"Command failed (exit {result.returncode}): {command}\n    {error_msg}",
          node)
    return result.stdout.strip() if capture else None

  def _log_step(self, tag: str, message: str, c: str = "green"):
    prefix = color(f"[{tag}]", c)
    print(f"  {prefix} {message}")

  def execute_nodes(self):
    start_time = time.time()

    if self.target:
      if self.target not in self._targets:
        self._execute_node_list(self.build_nodes)
        if self.target in self._targets:
          self._execute_node_list(self._targets[self.target])
        else:
          raise BuildError(f"Target '{self.target}' not found. Available: {', '.join(self._targets.keys()) or 'none'}")
      else:
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
    self.current_build = self._interpolate(node.args)
    print(f"\n{color('>>> Build Target:', 'bold')} {color(self.current_build, 'cyan')}")

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
    value = self._interpolate(parts[1].strip()).strip('"')
    self.context[key] = value
    self._log_step("set", f"{key} = {color(value, 'cyan')}")

  def _exec_export(self, node: BuildSystemNode):
    parts = node.args.split(" ", 1)
    if len(parts) != 2:
      raise BuildError(f"Invalid export command: {node.args}. Expected: export <key> <value>", node)

    key = parts[0].strip()
    value = self._interpolate(parts[1].strip()).strip('"')
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
    message = self._interpolate(node.args).strip('"')
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
      shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
      if not os.path.exists(src):
        raise BuildError(f"Source not found: {src}", node)
      os.makedirs(os.path.dirname(dst), exist_ok=True)
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
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)

  def _exec_delete(self, node: BuildSystemNode):
    tokens = shlex.split(node.args)
    if not tokens:
      raise BuildError(f"Invalid delete command: {node.args}", node)

    for target in tokens:
      path = os.path.join(self.base_dir, self._interpolate(target))
      self._log_step("delete", target, "red")
      if self.dry_run:
        print(color(f"    [dry-run] would delete {path}", "yellow"))
        continue
      if os.path.isdir(path):
        shutil.rmtree(path)
      elif os.path.exists(path):
        os.remove(path)
      else:
        raise BuildError(f"Path not found: {path}", node)

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

    if len(tokens) == 1:
      value = self._interpolate(tokens[0])
      if value != tokens[0]:
        result = bool(value) and value.lower() not in ("false", "0", "no", "")
      else:
        result = tokens[0] in self.context or tokens[0] in self.env
    elif len(tokens) == 3:
      left = self._interpolate(tokens[0]).strip('"')
      op = tokens[1]
      right = self._interpolate(tokens[2]).strip('"')
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
    # what the fuck is this
    commands = []
    for n in body:
      if n.command != "run":
        raise BuildError(f"Only 'run' commands are allowed inside parallel blocks, got: {n.command!r}", n)
      tokens = shlex.split(n.args)
      commands.append((" ".join(tokens), self.base_dir, n))

    self._log_step("parallel", f"executing {len(commands)} commands", "blue")
    
    if self.dry_run:
      for cmd, cwd, n in commands:
        print(color(f"    [dry-run] would execute in parallel: {cmd}", "yellow"))
      return

    results = [None] * len(commands)
    errors = [None] * len(commands)

    def run_cmd(idx, cmd, cwd, n):
      try:
        result = subprocess.run(
            self._interpolate(cmd), shell=True, cwd=cwd,
            capture_output=True, text=True,
            env={**os.environ, **self.env},
        )
        results[idx] = result
        if result.returncode != 0:
          errors[idx] = BuildError(
              f"Parallel command failed (exit {result.returncode}): {cmd}\n    {result.stderr.strip()}", n)
      except Exception as e:
        errors[idx] = BuildError(f"Parallel command exception: {e}", n)

    threads = []
    for i, (cmd, cwd, n) in enumerate(commands):
      t = threading.Thread(target=run_cmd, args=(i, cmd, cwd, n))
      threads.append(t)
      t.start()

    for t in threads:
      t.join()

    for i, (cmd, _, _) in enumerate(commands):
      r = results[i]
      status = color("OK", "green") if r and r.returncode == 0 else color("FAIL", "red")
      print(f"    {status} {cmd}")
      if r and r.stdout:
        for line in r.stdout.strip().split("\n"):
          print(f"      {line}")

    first_error = next((e for e in errors if e), None)
    if first_error:
      raise first_error

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
    # does this even work??
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
    command = parts[1].strip().strip('"')

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
    self.context[var_name] = " ".join(relative)
    self._log_step("glob", f"{var_name} = [{', '.join(relative)}] ({len(relative)} matches)", "blue")

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
    message = self._interpolate(node.args).strip('"')
    self._log_step("warn", message, "yellow")

  def _exec_error(self, node: BuildSystemNode):
    message = self._interpolate(node.args).strip('"')
    self._log_step("error", message, "red")
    raise BuildError(message, node)

  def _exec_debug(self, node: BuildSystemNode):
    if self.verbose:
      message = self._interpolate(node.args).strip('"')
      self._log_step("debug", message, "dim")

  def _print_summary(self, total_time: float):
    print(f"\n{color('=== Build Summary ===', 'bold')}")
    print(f"  Target:    {color(self.current_build or 'none', 'cyan')}")
    print(f"  Steps:     {self._step_count}")
    if self._failed_steps:
      print(f"  Failed:    {color(str(self._failed_steps), 'red')}")
    print(f"  Duration:  {color(f'{total_time:.3f}s', 'green')}")
    print(f"  Status:    {color('COMPLETE', 'green') if not self._failed_steps else color('PARTIAL', 'yellow')}")

    if self.context:
      print(f"\n  {color('Context:', 'bold')}")
      for key, value in self.context.items():
        print(f"    {key} = {color(str(value), 'cyan')}")

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
  builder --list                List available targets and functions
  builder --plan                Show the execution plan without running

{color('keywords:', 'dim')}
  build, set, export, echo, run, capture, install, from, import,
  copy, move, delete, mkdir, glob, if/elif/else/endif,
  foreach/endforeach, fn/endfn, target/endtarget, invoke,
  parallel/endparallel, try/catch/endtry, require, include,
  exit, warn, error, debug
      """,
  )
  parser.add_argument("positional", nargs="*", help="Build file (.build) and/or target name")
  parser.add_argument("--dry-run", action="store_true", help="Show steps without executing")
  parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
  parser.add_argument("-t", "--target", type=str, default=None, help="Run a specific target")
  parser.add_argument("--list", action="store_true", help="List available targets and functions")
  parser.add_argument("--plan", action="store_true", help="Show execution plan without running")

  args = parser.parse_args()

  build_file, positional_target = _resolve_positional(args.positional)
  target = args.target or positional_target

  interpreter = BuildSystemInterpreter(
      f_name=build_file,
      dry_run=args.dry_run or args.plan,
      verbose=args.verbose,
      target=target,
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
      return

    if args.plan:
      print(f"\n{color('Execution Plan:', 'bold')}")
      print(f"  File:   {color(build_file, 'cyan')}")
      if target:
        print(f"  Target: {color(target, 'magenta')}")
      print(f"  Steps:  {len(interpreter.build_nodes)} top-level nodes")
      if interpreter._targets:
        print(f"\n  {color('Targets:', 'bold')}")
        for name, body in interpreter._targets.items():
          marker = color(" <--", "green") if name == target else ""
          print(f"    {color(name, 'magenta')}  ({len(body)} steps){marker}")
      if interpreter._functions:
        print(f"\n  {color('Functions:', 'bold')}")
        for name, body in interpreter._functions.items():
          print(f"    {color(name, 'magenta')}  ({len(body)} steps)")
      if target and target not in interpreter._targets:
        available = ', '.join(interpreter._targets.keys()) if interpreter._targets else 'none'
        print(f"\n{color('Error:', 'red')} Target '{target}' not found. Available: {available}")
        sys.exit(1)

      print(f"\n  {color('Steps:', 'bold')}")
      nodes = interpreter._targets[target] if target else interpreter.build_nodes
      for i, node in enumerate(nodes, 1):
        cmd_display = node.args.split()[0] if node.command == "__call__" else node.command
        args_display = " ".join(node.args.split()[1:]) if node.command == "__call__" else node.args
        print(f"    {color(f'{i:3d}', 'dim')}  {color(cmd_display, 'blue')} {args_display}")
      print()
      return

    interpreter.execute_nodes()
  except SystemExit as e:
    sys.exit(e.code)
  except BuildError as e:
    print(f"\n{color('BUILD FAILED:', 'red')} {e}")
    sys.exit(1)
  except KeyboardInterrupt:
    print(f"\n{color('BUILD INTERRUPTED', 'yellow')}")
    sys.exit(130)


main()
