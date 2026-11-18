{ lib }:

config: agent:
let
  users = lib.attrValues (config.icedos.ai-tools.ai-accounts.users or { });
in
lib.any (u: ((u.agents.${agent} or { }).accounts or { }) != { }) users
