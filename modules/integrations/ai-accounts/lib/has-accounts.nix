{ lib }:

config: agent:
let
  users = lib.attrValues (config.icedos.ai-tools.ai-accounts.users or { });
  declares = a: lib.any (u: ((u.agents.${a} or { }).accounts or { }) != { }) users;
in
declares agent || (agent == "opencode" && declares "ollama")
