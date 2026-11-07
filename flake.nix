{
  description = "A lightweight abstraction for executing CLI coding agents headlessly";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";

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

      # x86_64-darwin intentionally omitted as deprecated upstream (Forgetful #85 precedent).
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

          # House style: pinned interpreter (requires-python = ">=3.12").
          python = pkgs.python312;

          pythonBase = pkgs.callPackage inputs.pyproject-nix.build.packages {
            inherit python;
          };

          uvLockedOverlay = workspace.mkPyprojectOverlay {
            sourcePreference = "wheel";
          };

          # Forgetful pattern: default dev version; release injects the real tag.
          # Pure builds (incl. `nix flake check`) always see "" -> "0.0.0+dev", never crash.
          # Release: AGENT_SHELL_VERSION=$(git describe --tags) nix build --impure
          version =
            let
              r = builtins.tryEval (builtins.getEnv "AGENT_SHELL_VERSION");
              e = if r.success then r.value else "";
            in
            if e != "" then e else "0.0.0+dev";

          # A9: the tree is ours, so allow-list exactly what the build reads.
          # Keeps the derivation hash stable across docs-only commits
          # (skill nix-build-cache-stability). Eval-time reads (uv.lock,
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

          # A9 source allow-list for the test run: tests read the package
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
            # Promote to default stage when the lint-config contribution lands
            # upstream (plan 54 task 341).
            ruff.enable = true;
            typos.enable = true;
            ruff.stages = [ "manual" ];
            typos.stages = [ "manual" ];
          };

          packages = {
            agent-shell-py = thisProject;
            default = thisProject;
          };

          devShells.default = pkgs.mkShell {
            packages = [
              devEnv
              pkgs.uv
              pkgs.ruff
            ]
            ++ config.pre-commit.settings.enabledPackages;
            env = {
              # uv manages the lockfile only; Nix owns the environment.
              UV_NO_SYNC = "1";
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              ${config.pre-commit.shellHook}
              export PYTHONPATH="$PWD/src:''${PYTHONPATH:-}"
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
            in
            {
              # Criterion: the build asserts zero runtime dependencies.
              zero-runtime-deps = smoke;
              # Unit + integration only; e2e excluded by design (real CLIs,
              # credentials, API cost). Sandbox has no network: any test that
              # needs it fails here (fail closed).
              inherit tests;
            };
        };
    };
}
