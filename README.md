 # Builder

 A modern build automation tool with a clean scripting language. Define variables, functions, targets, and control flow in `.build` files — then run them with a single command.

 ```
 builder deploy
 builder app.build test --verbose
 builder --plan
 ```

 ---

## Installation

**Requirements:** Python 3.11+

```bash
git clone https://github.com/TheMaxerius/builder.git
cd builder
pip install -e .
```

That's it. The `builder` command is now available globally.

```bash
builder --help
```

To update, pull the latest changes — no reinstall needed since it's an editable install.

```bash
git pull
```

### Ubuntu / Debian

Modern Ubuntu and Debian systems (22.04+) block `pip install` system-wide due to PEP 668. Use `pipx` instead, which handles the isolated environment automatically:

```bash
sudo apt install pipx
pipx ensurepath
```

Open a new terminal so the PATH update takes effect, then:

```bash
git clone https://github.com/TheMaxerius/builder.git
cd builder
pipx install -e .
```

If `builder` still isn't found, `~/.local/bin` may not be on your PATH. Add it permanently:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

---

## Getting Started

 Create a file called `main.build`:

 ```
 build myapp

 set version "1.0"
 export APP_ENV "production"

 echo "Building v${version}"

 fn compile
   run gcc -o app main.c
   echo "Compiled ${arg0}"
 endfn

 compile "main.c"
 ```

 Run it:

 ```bash
 builder
 ```

 ---

 ## CLI

 ```
 builder [buildfile] [target] [options]
 ```

 The CLI uses smart positional arguments. If an argument ends with `.build` or contains a path separator, it's treated as a build file. Otherwise, it's a target name.

 | Command | What it does |
 |---|---|
 | `builder` | Run `main.build` |
 | `builder deploy` | Run the `deploy` target from `main.build` |
 | `builder app.build` | Run `app.build` |
 | `builder app.build deploy` | Run `deploy` target from `app.build` |

 ### Options

 | Flag | Description |
 |---|---|
 | `--dry-run` | Show steps without executing anything |
 | `-v`, `--verbose` | Enable verbose logging, debug output, and step timings |
 | `-t`, `--target` | Run a specific target (alternative to positional) |
 | `--list` | List all available targets and functions |
 | `--plan` | Show the full execution plan without running |
 | `--watch` | Re-run build automatically when `.build` files change |
 | `--help` | Show help |

 ### Examples

 ```bash
 # Preview what a build would do
 builder --dry-run

 # See all targets in a build file
 builder app.build --list

 # Inspect the execution plan for a target
 builder --plan deploy

 # Run with full debug output
 builder test -v

 # Watch for changes and rebuild automatically
 builder --watch
 ```

 ---

 ## Language Reference

 ### Variables

 **`set`** stores build-local variables. They exist only within the build context and are accessed with `${name}`.

 ```
 set version "2.1"
 set output "./dist"
 echo "Version: ${version}"
 ```

 **`export`** sets environment variables. These are visible to all shell commands executed during the build.

 ```
 export NODE_ENV "production"
 export API_URL "https://api.example.com"
 run curl ${API_URL}/health
 ```

 **`append`** adds a value to an existing variable (space-separated).

 ```
 set flags ""
 append flags "-O2"
 append flags "-Wall"
 echo "Flags: ${flags}"   # -O2 -Wall
 ```

 **`env`** reads a variable from the OS environment with an optional fallback. The value is stored in the build context.

 ```
 env NODE_ENV "development"   # use $NODE_ENV if set, else "development"
 env DATABASE_URL              # empty string if not set
 echo "Mode: ${NODE_ENV}"
 ```

 **`dotenv`** loads variables from a `.env` file into the build context and into environment variables visible to all shell commands.

 ```
 dotenv ".env"                  # fail if not found
 dotenv ".env.local" optional   # skip silently if not found
 ```

 The `.env` file follows standard format — `KEY=value`, `KEY="quoted value"`. Comments (`# ...`) are ignored.

 Variables are interpolated anywhere with `${name}`. The interpreter checks build context first, then OS environment variables.

 ```
 set name "myapp"
 export APP_NAME "${name}"
 echo "Building ${APP_NAME}"
 ```

 #### Built-in Variables

 These are automatically set at startup. You can use them anywhere without declaring them.

 | Variable | Value |
 |---|---|
 | `${OS}` | `linux`, `darwin`, or `windows` |
 | `${ARCH}` | CPU architecture, e.g. `x86_64` or `arm64` |
 | `${CWD}` | Current working directory |
 | `${HOME}` | Home directory |
 | `${USER}` | Current user name |
 | `${DATE}` | Today's date — `YYYY-MM-DD` |
 | `${TIME}` | Current time — `HH:MM:SS` |
 | `${TIMESTAMP}` | Unix timestamp (seconds) |
 | `${BUILD_FILE}` | Absolute path to the current `.build` file |
 | `${BUILD_DIR}` | Directory containing the `.build` file |

 #### Interpolation Transforms

 Apply transforms directly inside `${...}` without creating intermediate variables:

 | Syntax | Result |
 |---|---|
 | `${upper:var}` | Uppercase: `HELLO` |
 | `${lower:var}` | Lowercase: `hello` |
 | `${trim:var}` | Strip leading/trailing whitespace |
 | `${len:var}` | Length of the string value |
 | `${var:-default}` | Value of `var`, or `default` if empty/unset |
 | `${var:+word}` | `word` if `var` is set and non-empty, else `""` |
 | `${env:VAR}` | Read directly from OS environment |
 | `${env:VAR:fallback}` | OS environment variable with fallback |

 ```
 set tag "  v1.2.3  "
 echo "${trim:tag}"           # v1.2.3
 echo "${upper:tag}"          # V1.2.3
 echo "${len:tag}"            # 9 (after trim, not auto-applied)

 set mode ""
 echo "${mode:-production}"   # production (mode is empty)

 echo "${env:CI:false}"       # true on CI, false locally
 ```

 ---

 ### Functions

 Define reusable blocks with `fn` / `endfn`. Call them directly by name no special keyword needed.

 ```
 fn greet
   echo "Hello, ${arg0}!"
 endfn

 greet "world"
 greet "builder"
 ```

 Arguments are available as `${arg0}`, `${arg1}`, etc. The total count is `${argc}`.

 ```
 fn deploy_to
   echo "Deploying ${arg0} to ${arg1}"
   echo "Total args: ${argc}"
 endfn

 deploy_to "myapp" "production"
 ```

 Forward references are supported you can call a function before its definition in the file:

 ```
 compile "main.c"

 fn compile
   run gcc -o app ${arg0}
 endfn
 ```

 ---

 ### Targets

 Named groups of steps that can be invoked on demand or from the CLI.

 ```
 target deploy
   echo "Deploying..."
   run ./deploy.sh
 endtarget

 target clean
   delete ./dist
   delete ./build
 endtarget
 ```

 Run from the CLI:

 ```bash
 builder deploy
 builder clean --dry-run
 ```

 Or invoke from within a build file:

 ```
 invoke deploy
 ```

 ---

 ### Control Flow

 #### Conditionals

 ```
 if ${env} == "production"
   echo "Production mode"
 elif ${env} == "staging"
   echo "Staging mode"
 else
   echo "Development mode"
 endif
 ```

 Supported operators:

 | Operator | Example |
 |---|---|
 | `==` | `if ${a} == "hello"` |
 | `!=` | `if ${a} != "world"` |
 | `>`, `<`, `>=`, `<=` | `if ${score} > "50"` (string comparison) |
 | `contains` | `if ${path} contains "src"` |
 | `startswith` | `if ${name} startswith "app"` |
 | `endswith` | `if ${file} endswith ".py"` |
 | `matches` | `if ${ver} matches "^[0-9]+$"` (regex) |
 | `exists` | `if exists "./dist"` (file/directory exists) |

 Negate any condition with `not`:

 ```
 if not ${env} == "development"
   echo "Not in dev mode"
 endif
 ```

 Truthiness checks (variable exists and is non-empty):

 ```
 if debug_mode
   echo "Debug is enabled"
 endif
 ```

 #### Loops

 ```
 foreach module in auth api core utils
   echo "Building ${module}"
   run make -C ${module}
 endforeach
 ```

 #### Error Handling

 Wrap risky commands in `try` / `catch` / `endtry`. The error message is available as `${_error}`.

 ```
 try
   run ./risky-command.sh
 catch
   warn "Command failed: ${_error}"
   run ./fallback.sh
 endtry
 ```

 Blocks can be nested:

 ```
 try
   try
     run ./primary.sh
   catch
     run ./secondary.sh
   endtry
 catch
   error "Both primary and secondary failed"
 endtry
 ```

 #### Requirements

 Fail the build immediately if a precondition isn't met:

 ```
 require ${env} == "production" || "Expected production environment"
 require api_key || "API_KEY must be set"
 ```

 **`check`** asserts that a command is installed and available on `PATH`. Use it to verify your toolchain before the build starts.

 ```
 check node         || "Node.js is required — https://nodejs.org"
 check python3      || "Python 3 is required"
 check docker
 ```

 On success it prints the resolved path. On failure it raises a build error with your custom message (or a default one).

 **`port free`** checks whether a TCP port is available (nothing is listening on it). Like `check`, it supports a custom error message and can store its result instead of failing.

 ```
 # Hard check — fail if busy
 port free 3000
 port free ${PORT} || "Port ${PORT} is already in use on ${OS}"

 # Soft check — store "free" or "" and continue either way
 port free 3000 → api_port_free
 if not api_port_free
   warn "Port 3000 is taken — you may need to stop another process"
 endif

 # Via set
 set api_slot port free 8080
 if api_slot
   echo "Port 8080 is free, starting server"
 endif
 ```

 The stored value is `"free"` when the port is available, or `""` (empty) when it is in use — so `if port_var` and `if not port_var` work naturally.

 ---

 ### npm and Cargo

 **`npm`** runs any npm subcommand. Append `in "<dir>"` to run it in a subdirectory.

 ```
 npm install
 npm ci
 npm run build
 npm run dev in "frontend"
 npm install in "packages/web"
 npm test in "apps/api"
 ```

 **`cargo`** runs any Cargo subcommand. Same `in "<dir>"` suffix works identically.

 ```
 cargo build
 cargo build --release
 cargo build --release in "api"
 cargo test in "crates/core"
 cargo check
 cargo clippy
 cargo fmt
 ```

 Both commands log with their own `[npm]` / `[cargo]` step label and accept `${variables}` anywhere in the arguments.

 ---

 ### Reading Build Manifests

 **`json`** reads a value from any JSON file and stores it in a variable. Supports dotted key paths. The variable name defaults to the last key segment.

 ```
 json version from "package.json"                # → ${version}
 json name    from "package.json" as app_name    # → ${app_name}
 json scripts.build from "package.json" as build_cmd
 json engines.node  from "package.json" as node_req
 ```

 **`toml`** reads a value from any TOML file. Bare keys automatically look inside `[package]`, making it ergonomic for `Cargo.toml`.

 ```
 toml version from "Cargo.toml"                   # → ${version}  (reads [package].version)
 toml name    from "Cargo.toml" as crate_name      # → ${crate_name}
 toml dependencies.serde from "Cargo.toml"         # → ${serde}  (reads [dependencies].serde)
 toml workspace.resolver from "Cargo.toml" as res
 ```

 ---

 ### Semver Comparisons

 Version strings can be compared in `if` conditions using `semver` operators. Leading `v` is stripped automatically and pre-release labels are ignored.

 ```
 if ${NODE_VERSION} semver< "18.0.0"
     error "Node.js 18+ required (found ${NODE_VERSION})"
 endif

 if ${CARGO_VERSION} semver>= "1.70.0"
     echo "Cargo 1.70+ — sparse registry enabled"
 endif
 ```

 Available operators: `semver==`, `semver!=`, `semver>`, `semver>=`, `semver<`, `semver<=`.

 ---

 ### Tool Version Variables

 The following variables are auto-detected at startup (empty string if the tool is not installed):

 | Variable | Source |
 |---|---|
 | `${NODE_VERSION}` | `node --version` |
 | `${NPM_VERSION}` | `npm --version` |
 | `${PNPM_VERSION}` | `pnpm --version` |
 | `${YARN_VERSION}` | `yarn --version` |
 | `${BUN_VERSION}` | `bun --version` |
 | `${RUSTC_VERSION}` | `rustc --version` |
 | `${CARGO_VERSION}` | `cargo --version` |
 | `${PYTHON_VERSION}` | `python3 --version` |
 | `${GO_VERSION}` | `go version` |
 | `${DENO_VERSION}` | `deno --version` |

 Use `if not NODE_VERSION` / `if not CARGO_VERSION` to branch on whether a tool is installed.

 ---

 ### Shell Commands

 **`run`** executes a shell command:

 ```
 run npm install
 run make -j4
 ```

 **`capture`** runs a command and stores its stdout in a variable:

 ```
 capture git_hash "git rev-parse --short HEAD"
 echo "Commit: ${git_hash}"
 ```

 **`from`** runs a command in a specific directory:

 ```
 from frontend run "npm run build"
 from backend run "cargo build --release"
 ```

 **`timeout`** runs a command with a time limit. The build fails if the command does not finish in time.

 ```
 timeout 30 run "npm install"
 timeout 60 from "backend" run "cargo build --release"
 ```

 **`retry`** retries a failing command up to N times with exponential back-off (0.5s, 1.0s, 1.5s, capped at 3s).

 ```
 retry 3 run "curl -f https://api.example.com/health"
 retry 5 from "infra" run "terraform apply -auto-approve"
 ```

 ---

 ### Parallel Execution

 Run multiple shell commands concurrently:

 ```
 parallel
   run npm run build:css
   run npm run build:js
   run npm run build:images
 endparallel
 ```

 Each command runs in its own thread. Results are collected and displayed with OK/FAIL status. If any command fails, the build fails after all commands complete.

 ---

 ### File Operations

 ```
 mkdir ./dist ./dist/assets

 copy src/config.json dist/config.json
 move dist/old.js dist/archive/old.js
 delete dist/temp ./cache

 glob styles "src/**/*.css"
 echo "Found: ${styles}"
 ```

 | Command | Description |
 |---|---|
 | `mkdir <paths...>` | Create directories (recursive) |
 | `copy <src> <dst>` | Copy files or directories |
 | `move <src> <dst>` | Move / rename |
 | `delete <paths...>` | Delete files or directories |
 | `glob <var> <pattern>` | Match files by pattern, store in variable |

 ---

 ### Composition

 **`include`** pulls in another build file. Functions and targets defined in included files become available. Circular includes are detected and raise an error.

 ```
 include shared/common.build
 include ci/deploy.build
 ```

 **`import`** registers a file alias for reference:

 ```
 import config ./config/app.json
 ```

 **`install`** runs a package manager:

 ```
 install pip requirements.txt
 install npm express
 install apt libssl-dev
 ```

 ---

 ### Logging

 ```
 echo "Normal output"
 debug "Only visible with --verbose"
 warn "Something might be wrong"
 error "Fatal — stops the build"
 ```

 | Command | Behavior |
 |---|---|
 | `echo` | Always printed |
 | `debug` | Only printed with `--verbose` |
 | `warn` | Printed with yellow highlight |
 | `error` | Printed with red highlight, stops the build |

 **`section`** prints a bold section header that visually separates phases of a long build. Useful for readability in CI logs.

 ```
 section "Toolchain Check"
 check node || "Node.js is required"
 check python3

 section "Building"
 from frontend run "npm run build"
 from backend run "cargo build --release"

 section "Deploy"
 invoke deploy
 ```

 ---

 ### Other

 **`build`** declares the build name (shown in output and summary):

 ```
 build myapp
 ```

 **`exit`** stops the build:

 ```
 exit 0    # success
 exit 1    # failure
 ```

 ---

 ## Build Summary

 Every build prints a summary at the end:

 ```
 === Build Summary ===
   Target:    myapp
   Steps:     47
   Duration:  0.119s
   Status:    COMPLETE

   Context:
     version = 2.1
     output = ./dist

   Targets:
     deploy (3 steps)
     clean (2 steps)

   Functions:
     greet (1 steps)
 ```

 With `--verbose`, per-step timings are also displayed, color-coded green/yellow/red by duration.

 ---



 ---

 ## Full Keyword List

 | Category | Keywords |
 |---|---|
 | Variables | `set`, `export`, `append`, `env`, `dotenv` |
 | Output | `echo`, `debug`, `warn`, `error`, `section` |
 | Shell | `run`, `capture`, `from`, `timeout`, `retry` |
 | Files | `copy`, `move`, `delete`, `mkdir`, `glob` |
 | Assertions | `require`, `check`, `port` |
 | npm / Node | `npm`, `json` |
 | Rust / Cargo | `cargo`, `toml` |
 | Control | `if`, `elif`, `else`, `endif`, `foreach`, `endforeach` |
 | Error handling | `try`, `catch`, `endtry` |
 | Functions | `fn`, `endfn` |
 | Targets | `target`, `endtarget`, `invoke` |
 | Concurrency | `parallel`, `endparallel`, `spawn`, `endspawn` |
 | Composition | `include`, `import`, `install` |
 | Build | `build`, `exit` |
