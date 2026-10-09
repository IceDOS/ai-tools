{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.agent-launch =
    let
      inherit (icedosLib)
        mkBoolOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        ;

      inherit ((lib.importTOML ./config.toml).icedos.ai-tools.agent-launch)
        agents
        defaultAgent
        desktopEntry
        ;
    in
    {
      # Agents in the picker; each agent module registers itself here.
      agents = mkSubmoduleAttrsOption { default = agents; } {
        command = mkStrListOption { default = [ ]; };
        label = mkStrOption { default = ""; };
        # Session resume data for extensions such as the zed agent bridge.
        resumeArgs = mkStrListOption { default = [ ]; };
        locator = mkStrOption { default = ""; };
      };

      # Agent started without asking; "" shows the picker every time.
      defaultAgent = mkStrOption { default = defaultAgent; };

      # Whether the launcher installs a desktop entry opening in the default terminal.
      desktopEntry = mkBoolOption { default = desktopEntry; };
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        { config, pkgs, ... }:

        let
          cfg = config.icedos.ai-tools.agent-launch;

          launcherConfig = pkgs.writeText "agent-launch.json" (
            builtins.toJSON { inherit (cfg) agents defaultAgent; }
          );

          agentLaunch = pkgs.writeShellScriptBin "agent-launch" ''
            export PATH=${lib.makeBinPath [ pkgs.fzf ]}"''${PATH:+:$PATH}"
            exec ${pkgs.python3Minimal}/bin/python3 ${./lib/agent_launch.py} --config ${launcherConfig} "$@"
          '';

          # Terminal=true: the desktop environment launches the user's default terminal.
          desktopItem = pkgs.makeDesktopItem {
            name = "agent-launch";
            desktopName = "Agent Launcher";
            comment = "Pick a coding agent and start it";
            icon = "utilities-terminal";
            exec = "${agentLaunch}/bin/agent-launch";
            terminal = true;
            categories = [
              "Development"
              "Utility"
            ];
          };
        in
        {
          environment.systemPackages = [ agentLaunch ] ++ lib.optional cfg.desktopEntry desktopItem;

          icedos.system.tips.list = [
            "agent-launch opens a picker of your coding agents and starts the one you choose."
          ];
        }
      )
    ];

  meta.name = "agent-launch";
}
