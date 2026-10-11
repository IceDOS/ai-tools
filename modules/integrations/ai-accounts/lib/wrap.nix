{ lib, pkgs }:

{
  agent,
  package,
  bin,
}:

let
  aiAccount = lib.getExe (pkgs.callPackage ../package.nix { });
in
pkgs.symlinkJoin {
  name = "${package.pname or package.name}-ai-accounts";
  version = package.version or "";
  paths = [ package ];
  nativeBuildInputs = [ pkgs.makeWrapper ];
  postBuild = ''
    rm "$out/bin/${bin}"
    makeWrapper ${aiAccount} "$out/bin/${bin}" --add-flags "exec ${agent} -- ${package}/bin/${bin}"
  '';
  passthru = package.passthru or { };
  meta = (package.meta or { }) // {
    mainProgram = bin;
  };
}
