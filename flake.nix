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
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      # x86_64-darwin intentionally omitted as deprecated upstream (Forgetful #85 precedent).
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      perSystem =
        { pkgs, ... }:
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

          thisProject = pythonSet.agent-shell-py;

          # Runtime env from the lock (default groups): the library alone.
          runtimeEnv = pythonSet.mkVirtualEnv "agent-shell-env" workspace.deps.default;

          # Full locked dev environment: pytest + pytest-asyncio from uv.lock.
          devEnv = pythonSet.mkVirtualEnv "agent-shell-dev-env" workspace.deps.all;
        in
        {
          packages = {
            agent-shell-py = thisProject;
            default = thisProject;
          };

          devShells.default = pkgs.mkShell {
            packages = [
              devEnv
              pkgs.uv
              pkgs.ruff
            ];
            env = {
              # uv manages the lockfile only; Nix owns the environment.
              UV_NO_SYNC = "1";
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''export PYTHONPATH="$PWD/src:''${PYTHONPATH:-}"'';
          };

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
            };
        };
    };
}
