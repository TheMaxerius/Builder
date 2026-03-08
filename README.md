 # Builder

 A modern build automation tool with a clean scripting language. Define variables, functions, targets, and control flow in `.build` files — then run them with a single command.

 ```
 builder deploy
 builder app.build test --verbose
 builder --plan
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
 python main.py
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

 Variables are interpolated anywhere with `${name}`. The interpreter checks build context first, then environment variables.

 ```
 set name "myapp"
 export APP_NAME "${name}"
 echo "Building ${APP_NAME}"
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
 | Variables | `set`, `export` |
 | Output | `echo`, `debug`, `warn`, `error` |
 | Shell | `run`, `capture`, `from` |
 | Files | `copy`, `move`, `delete`, `mkdir`, `glob` |
 | Control | `if`, `elif`, `else`, `endif`, `foreach`, `endforeach` |
 | Error handling | `try`, `catch`, `endtry`, `require` |
 | Functions | `fn`, `endfn` |
 | Targets | `target`, `endtarget`, `invoke` |
 | Composition | `parallel`, `endparallel`, `include`, `import`, `install` |
 | Build | `build`, `exit` |
