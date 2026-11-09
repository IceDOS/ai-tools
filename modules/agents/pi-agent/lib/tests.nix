# Run: nix eval --impure --expr 'import ./lib/tests.nix' (expects [ ])
let
  lib = (builtins.getFlake "nixpkgs").lib;
  dataDir = import ./data-dir.nix { inherit lib; };
  mcp = import ./mcp-json.nix { inherit lib; };
  ext = import ./extensions.nix { inherit lib; };
  exists =
    p:
    builtins.elem p [
      "/ex/plan-mode/index.ts"
      "/ex/todo.ts"
    ];
  user = {
    home = "/home/u";
    configHome = "/home/u/.config";
    dataHome = "/home/u/.local/share";
  };
  render =
    servers:
    (mcp.render {
      inherit servers;
      timeout = 900;
    }).mcpServers;
in
lib.runTests {
  testDataDirDefault = {
    expr = dataDir.expand (user // { raw = ""; });
    expected = "/home/u/.config/pi-agent";
  };
  testDataDirTildeAndSlashes = {
    expr = dataDir.expand (user // { raw = "~//agents/pi/"; });
    expected = "/home/u/agents/pi";
  };
  testDataDirDataHome = {
    expr = dataDir.expand (user // { raw = "$XDG_DATA_HOME/pi"; });
    expected = "/home/u/.local/share/pi";
  };
  testShellDefault = {
    expr = dataDir.shell "";
    expected = "\${XDG_CONFIG_HOME:-$HOME/.config}/pi-agent";
  };
  testShellTilde = {
    expr = dataDir.shell "~/pi";
    expected = "$HOME/pi";
  };
  testStdio = {
    expr = render {
      a = {
        command = "x";
        args = [ "y" ];
        env = { };
        url = null;
      };
    };
    expected.a = {
      command = "x";
      args = [ "y" ];
      timeout = 900;
    };
  };
  testStdioEnvCwd = {
    expr = render {
      a = {
        command = "x";
        env.K = "v";
        cwd = "/w";
      };
    };
    expected.a = {
      command = "x";
      env.K = "v";
      cwd = "/w";
      timeout = 900;
    };
  };
  testHttpBearer = {
    expr = render {
      h = {
        command = null;
        url = "http://127.0.0.1:1/mcp";
        bearerTokenEnvVar = "TOK";
      };
    };
    expected.h = {
      url = "http://127.0.0.1:1/mcp";
      headers.Authorization = "Bearer \${TOK}";
      timeout = 900;
    };
  };
  testExplicitAuthorizationWins = {
    expr = render {
      h = {
        url = "http://x/mcp";
        bearerTokenEnvVar = "TOK";
        headers.Authorization = "Basic abc";
      };
    };
    expected.h = {
      url = "http://x/mcp";
      headers.Authorization = "Basic abc";
      timeout = 900;
    };
  };
  testDisabledKept = {
    expr = render {
      d = {
        url = "http://x/mcp";
        disabled = true;
      };
    };
    expected.d = {
      url = "http://x/mcp";
      timeout = 900;
      enabled = false;
    };
  };
  testInvalidNames = {
    expr = mcp.invalidNames {
      "ok_1-x" = { };
      "bad.name" = { };
    };
    expected = [ "bad.name" ];
  };
  testBuiltinDir = {
    expr = ext.resolveBuiltin {
      examplesDir = "/ex";
      name = "plan-mode";
      inherit exists;
    };
    expected = "/ex/plan-mode";
  };
  testBuiltinFile = {
    expr = ext.resolveBuiltin {
      examplesDir = "/ex";
      name = "todo";
      inherit exists;
    };
    expected = "/ex/todo.ts";
  };
  testBuiltinMissing = {
    expr = ext.missingBuiltins {
      examplesDir = "/ex";
      names = [
        "todo"
        "nope"
        "plan-mode"
      ];
      inherit exists;
    };
    expected = [ "nope" ];
  };
  testOrder = {
    expr = ext.order {
      builtin = [ "/b" ];
      shipped = [
        "/s1"
        "/s2"
      ];
      inline = {
        zeta = "/iz";
        alpha = "/ia";
      };
    };
    expected = [
      "/b"
      "/s1"
      "/s2"
      "/ia"
      "/iz"
    ];
  };
  testInlineNames = {
    expr = ext.invalidInlineNames [
      "alimit"
      "llamacpp-lifecycle"
      "bad name"
      "a/b"
      "ok_1"
    ];
    expected = [
      "bad name"
      "a/b"
    ];
  };
  testWindows = {
    expr = ext.invalidWindows [
      "1h"
      "24h"
      "1M"
      "0d"
      "15"
      "2x"
      "30d"
    ];
    expected = [
      "0d"
      "15"
      "2x"
    ];
  };
}
