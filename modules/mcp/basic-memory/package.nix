{
  pkgs,
  # Shell run before every entry point, e.g. to export BASIC_MEMORY_* defaults.
  preExec ? "",
}:

let
  # Pins refreshed by ./update.sh.
  source = pkgs.lib.importJSON ./source.json;

  python = import ../lib/python.nix {
    inherit pkgs;

    # basic-memory pins an exact FastMCP release; update.sh follows that pin.
    extraOverrides = final: prev: {
      fastmcp-slim = final.mkWheel {
        pname = "fastmcp-slim";
        inherit (source.fastmcp-slim) version url hash;
        dependencies =
          with final;
          [
            # base
            mcp-types
            platformdirs
            pydantic
            pydantic-settings
            python-dotenv
            rich
            typing-extensions
            # client and server extras
            authlib
            cyclopts
            exceptiongroup
            griffelib
            httpx2
            joserfc
            jsonref
            jsonschema-path
            mcp
            openapi-pydantic
            opentelemetry-api
            packaging
            py-key-value-aio
            pyperclip
            python-multipart
            pyyaml
            starlette
            uncalled-for
            uvicorn
            watchfiles
            websockets
          ]
          ++ pydantic.optional-dependencies.email
          ++ py-key-value-aio.optional-dependencies.keyring
          ++ py-key-value-aio.optional-dependencies.filetree
          ++ py-key-value-aio.optional-dependencies.memory;
      };

      fastmcp = final.mkWheel {
        pname = "fastmcp";
        inherit (source.fastmcp) version url hash;
        dependencies = [ final.fastmcp-slim ];
      };

      # Exact upstream pin; 0.162 adds a telemetry module that needs opentelemetry-api.
      fastapi = prev.fastapi.overridePythonAttrs (old: {
        inherit (source.fastapi) version;
        src = prev.fastapi.src.override {
          tag = source.fastapi.version;
          inherit (source.fastapi) hash;
        };
        dependencies = old.dependencies ++ [ final.opentelemetry-api ];
        # Wants >=1.44; nixpkgs has 1.43, which has every API the module imports.
        pythonRelaxDeps = (old.pythonRelaxDeps or [ ]) ++ [ "opentelemetry-api" ];
        doCheck = false;
      });

      # Not in nixpkgs; imported as redis_fastapi by the API app.
      fastapi-redis-sdk = final.mkWheel {
        pname = "fastapi-redis-sdk";
        inherit (source.fastapi-redis-sdk) version url hash;
        dependencies = with final; [
          anyio
          fastapi
          pydantic
          pydantic-settings
          redis
        ];
      };
    };
  };

  # Built from the release source, which also carries the skills/ the module reads.
  basic-memory = python.pkgs.buildPythonPackage rec {
    pname = "basic-memory";
    inherit (source) version;
    pyproject = true;

    src = pkgs.fetchFromGitHub {
      owner = "basicmachines-co";
      repo = "basic-memory";
      tag = source.rev;
      inherit (source) hash;
    };

    # uv-dynamic-versioning reads git tags, which the fetched source lacks.
    env.UV_DYNAMIC_VERSIONING_BYPASS = version;

    # typer._click exists only in Typer 0.26+; nixpkgs Typer still uses plain Click.
    postPatch = ''
      substituteInPlace src/basic_memory/cli/commands/man.py \
        --replace-fail "from typer._click.core import" "from click.core import"
    '';

    build-system = with python.pkgs; [
      hatchling
      uv-dynamic-versioning
    ];

    dependencies =
      with python.pkgs;
      [
        aiofiles
        aiosqlite
        alembic
        anyio
        asyncpg
        dateparser
        fastapi
        fastapi-redis-sdk
        fastembed
        fastmcp
        filelock
        greenlet
        httpx
        logfire
        loguru
        markdown-it-py
        mcp
        mdformat
        mdformat-frontmatter
        mdformat-gfm
        openai
        pathspec
        pillow
        psutil
        psycopg
        pybars3
        pydantic
        pydantic-settings
        pyjwt
        python-dotenv
        python-frontmatter
        pyyaml
        rich
        sniffio
        sqlalchemy
        sqlite-vec
        typer
        unidecode
        uvloop
        watchfiles
      ]
      ++ fastapi.optional-dependencies.standard
      ++ pydantic.optional-dependencies.email
      ++ [ tzdata ];

    # Dev and test tools listed as runtime deps upstream; the code never imports them.
    pythonRemoveDeps = [
      "pyright"
      "pytest-aio"
      "pytest-asyncio"
    ];
    # nixpkgs differs from upstream's exact pins.
    pythonRelaxDeps = [
      "psycopg"
      "pyjwt"
    ];

    pythonImportsCheck = [ "basic_memory" ];
    doCheck = false;
  };

  env = python.withPackages (_: [ basic-memory ]);

  mkBin =
    name:
    import ../lib/python-bin.nix {
      inherit
        pkgs
        env
        name
        preExec
        ;
      entry = "basic_memory.cli.main:app";
    };
in
pkgs.symlinkJoin {
  name = "basic-memory-${basic-memory.version}";
  paths = [
    (mkBin "basic-memory")
    (mkBin "bm")
  ];
  passthru = {
    inherit (basic-memory) src;
    unwrapped = basic-memory;
  };
  meta.mainProgram = "basic-memory";
}
