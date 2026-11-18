{
  description = "A lightweight abstraction for executing CLI coding agents headlessly";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        pyproject-nix.follows = "pyproject-nix";
      };
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
      };
    };

    git-hooks-nix = {
      url = "github:cachix/git-hooks.nix";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        flake-compat.follows = "flake-compat";
      };
    };

    # default.nix / shell.nix entry points for nix-build and nix-shell
    flake-compat = {
      url = "github:NixOS/flake-compat";
      flake = false;
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [ inputs.git-hooks-nix.flakeModule ];

      # x86_64-darwin is omitted: nixpkgs deprecated it, so there is no build
      # we could claim to support there. The other three are declared as intent,
      # not as verification - see the Architectures row in docs/nix.md.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      perSystem =
        { config, pkgs, ... }:
        let
          inherit (pkgs) lib;

          workspace = inputs.uv2nix.lib.workspace.loadWorkspace {
            workspaceRoot = ./.;
          };

          # Pin the interpreter rather than inheriting the channel default: the
          # package declares requires-python >= 3.12, so what it builds against
          # should not drift when the channel moves.
          python = pkgs.python312;

          pythonBase = pkgs.callPackage inputs.pyproject-nix.build.packages {
            inherit python;
          };

          uvLockedOverlay = workspace.mkPyprojectOverlay {
            sourcePreference = "wheel";
          };

          # Version comes from the environment, never from git, because a pure
          # build has no .git to read: the flake source is a store path and
          # hatch-vcs would fail outright. Unset means a development build, so
          # `nix flake check` and any other pure evaluation succeeds instead of
          # crashing. A release stamps the tag explicitly and accepts the
          # impurity of reading an env var:
          #   AGENT_SHELL_VERSION=$(git describe --tags --abbrev=0) nix build --impure
          version =
            let
              r = builtins.tryEval (builtins.getEnv "AGENT_SHELL_VERSION");
              e = if r.success then r.value else "";
            in
            if e != "" then e else "0.0.0+dev";

          # Allow-list, not a filter: the build sees exactly these paths, so a
          # new file at the repo root cannot enter the derivation by accident
          # and change its hash. That is what keeps the derivation hash stable
          # across docs-only commits, which in turn keeps binary-cache and
          # rebuild decisions meaningful. Eval-time reads (uv.lock,
          # pyproject.toml by loadWorkspace) use the raw path, not this source.
          packageSource = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./README.md
              ./LICENSE
              ./src
            ];
          };

          # The flake source carries no .git, so hatch-vcs cannot resolve a
          # version at build time; stamp it from the env-driven version above.
          myOverrides = _final: prev: {
            agent-shell-py = prev.agent-shell-py.overrideAttrs (_old: {
              src = packageSource;
              SETUPTOOLS_SCM_PRETEND_VERSION = version;
            });
          };

          pythonSet = pythonBase.overrideScope (
            lib.composeManyExtensions [
              inputs.pyproject-build-systems.overlays.wheel
              uvLockedOverlay
              myOverrides
            ]
          );

          # Source allow-list for the test run: tests read the package
          # sources, pytest config, and the tests tree - nothing else.
          testSource = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./README.md
              ./LICENSE
              ./src
              ./tests
              ./examples
            ];
          };

          # Unit + integration suite in the sandbox: no network, no
          # credentials, no e2e. `-m "not e2e"` guards against an e2e-marked
          # test leaking into a unit/integration directory; scoping to those
          # two directories guards against unmarked files elsewhere.
          # tmux is a nativeBuildInput because the interactive-terminal
          # integration tests spawn it. Tests spawn fake CLIs via shebang
          # scripts; the suite uses sys.executable / shutil.which paths
          # (portable across NixOS, macOS, Linux, and the Nix sandbox).
          #
          # The architecture contracts run here too. They are as much a merge
          # gate as the tests: a change can keep every test green while
          # importing a concrete adapter from shared code, which is exactly
          # what the pyproject contracts forbid. CI runs them separately
          # (lint-imports in ci.yml / build.yml); running them here means a
          # Nix-only contributor gets the same verdict from `nix flake check`
          # instead of discovering it after pushing. import-linter is in
          # devEnv via workspace.deps.all, and testSource already carries
          # pyproject.toml (the contract definitions) plus src/ (the package
          # it analyses), so this needs no extra inputs.
          tests = pkgs.stdenv.mkDerivation {
            pname = "agent-shell-tests";
            inherit version;
            src = testSource;
            nativeBuildInputs = [
              devEnv
              pkgs.tmux
            ];
            phases = [
              "unpackPhase"
              "checkPhase"
              "installPhase"
            ];
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              export HOME="$TMPDIR"
              export PYTHONPATH="$PWD/src"
              lint-imports --no-cache
              pytest tests/unit tests/integration -m "not e2e" -q
              runHook postCheck
            '';
            installPhase = ''
              mkdir -p $out
              echo "unit + integration suite passed" > $out/passstamp
            '';
          };

          thisProject = pythonSet.agent-shell-py.overrideAttrs (old: {
            passthru = (old.passthru or { }) // {
              inherit tests;
            };
          });

          # Runtime env from the lock (default groups): the library alone.
          runtimeEnv = pythonSet.mkVirtualEnv "agent-shell-env" workspace.deps.default;

          # Full locked dev environment: pytest + pytest-asyncio from uv.lock.
          devEnv = pythonSet.mkVirtualEnv "agent-shell-dev-env" workspace.deps.all;
        in
        {
          pre-commit.settings.hooks = {
            nixfmt.enable = true;
            deadnix.enable = true;
            statix.enable = true;
            # Python + spell hooks are wired but run ONLY when invoked:
            #   pre-commit run ruff --hook-stage manual --all-files
            # The repo has no lint config, so a default-stage hook would fail
            # `nix flake check` on a clean checkout and impose enforcement on an
            # upstream that never opted in. `pre-push` is deliberately NOT used:
            # ruff's autofix rewrote 73 source files during a push (2026-11-07),
            # which is exactly the unrequested rewrite these stages must avoid.
            # Promote to the default stage only once a lint configuration has
            # been agreed with the maintainer. Until then a manual stage is the
            # honest setting: the tools are available and someone can run them,
            # but nothing implies the repository passes them.
            ruff.enable = true;
            typos.enable = true;
            ruff.stages = [ "manual" ];
            typos.stages = [ "manual" ];
          };

          packages = {
            agent-shell-py = thisProject;
            # The library inside a runnable python environment. Consumers that
            # want `python -c "import agent_shell"` to work with no PYTHONPATH
            # juggling should install this, not the bare site-packages output:
            # nixpkgs' python-with-packages only collects derivations it marks as
            # python modules, which a uv2nix package is not.
            env = runtimeEnv;
            default = thisProject;
          };

          devShells.default = pkgs.mkShell {
            packages = [
              devEnv
              pkgs.uv
            ]
            # ruff and typos arrive here through enabledPackages: a hook brings
            # its tool even at the manual stage. Listing pkgs.ruff again would
            # put two ruffs in the shell for no benefit.
            ++ config.pre-commit.settings.enabledPackages;
            env = {
              # uv manages the lockfile only; Nix owns the environment.
              UV_NO_SYNC = "1";
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              ${config.pre-commit.shellHook}
              # Anchor imports at the checkout root so `nix develop` works from
              # subdirectories. Append the inherited path only when nonempty;
              # an empty trailing component would add the caller's directory
              # to Python's import path and could shadow installed modules.
              repo_root="$(git rev-parse --show-toplevel)"
              if [ -n "''${PYTHONPATH:-}" ]; then
                export PYTHONPATH="$repo_root/src:$PYTHONPATH"
              else
                export PYTHONPATH="$repo_root/src"
              fi
            '';
          };

          formatter = pkgs.writeShellScriptBin "fmt" ''
            ${lib.getExe config.pre-commit.settings.package} run --all-files --config ${config.pre-commit.settings.configFile}
          '';

          checks =
            let
              smoke = pkgs.runCommand "agent-shell-import-smoke" { } ''
                ${runtimeEnv}/bin/python - <<'EOF'
                import importlib.metadata as md

                import agent_shell

                reqs = md.requires("agent-shell-py") or []
                assert not reqs, f"unexpected runtime dependencies: {reqs}"
                EOF
                touch $out
              '';

              # The suite runs against `src/` - it has to, because the import
              # contracts read pyproject.toml and analyse the sources - so on
              # its own it proves nothing about the packaged artifact. A module
              # the build forgot to install would pass every test and ship
              # broken. This imports the packaged tree with no source directory
              # on sys.path, so the only thing it can resolve is the build
              # output, and it checks the file set as well as importability.
              builtImport = pkgs.runCommand "agent-shell-built-import" { } ''
                unset PYTHONPATH
                export HOME="$TMPDIR"
                # One collation for sort and comm: they must agree or comm
                # reports its input as unsorted and the comparison is garbage.
                export LC_ALL=C

                # Completeness: every module the sources carry must exist in the
                # build. Compares file sets, because a module missing from the
                # build is invisible to any import-based check - it simply is
                # not there to be imported. One direction only: the build is
                # allowed to add files (hatch-vcs writes _version.py), it is not
                # allowed to lose them.
                src_modules=${./src}/agent_shell
                built_modules=${runtimeEnv}/${python.sitePackages}/agent_shell
                ( cd "$src_modules" && find . -name '*.py' | sort ) > "$TMPDIR/src.list"
                ( cd "$built_modules" && find . -name '*.py' | sort ) > "$TMPDIR/built.list"
                absent=$(comm -23 "$TMPDIR/src.list" "$TMPDIR/built.list")
                if [ -n "$absent" ]; then
                  echo "modules present in src/ but absent from the build:"
                  echo "$absent"
                  exit 1
                fi

                ${runtimeEnv}/bin/python - <<'EOF'
                import importlib
                import pkgutil

                import agent_shell

                # Resolve to the built environment, not to a source tree that
                # might be reachable some other way.
                assert "site-packages" in agent_shell.__file__, agent_shell.__file__

                # interactive_worker is a standalone script, not a package
                # module: it is spawned as a file and imports its siblings
                # directly so it can run with its own directory as sys.path[0].
                # Importing it the package way is not a supported usage, so it
                # is excluded here rather than changed to suit this check.
                script_entries = { "agent_shell.interactive_worker" }

                broken = [ ]
                for found in pkgutil.walk_packages(agent_shell.__path__, "agent_shell."):
                    if found.name in script_entries:
                        continue
                    try:
                        importlib.import_module(found.name)
                    except Exception as exc:
                        broken.append((found.name, repr(exc)))
                assert not broken, broken

                from agent_shell.shell import AgentShell
                from agent_shell.models.agent import AgentType

                print("built import ok:", agent_shell.__file__)
                EOF
                touch $out
              '';
            in
            {
              # The build asserts zero runtime dependencies.
              zero-runtime-deps = smoke;
              # The packaged module tree, imported with no source on sys.path.
              built-import = builtImport;
              # Unit + integration only; e2e excluded by design (real CLIs,
              # credentials, API cost). Sandbox has no network: any test that
              # needs it fails here (fail closed).
              inherit tests;
            };
        };
    };
}
