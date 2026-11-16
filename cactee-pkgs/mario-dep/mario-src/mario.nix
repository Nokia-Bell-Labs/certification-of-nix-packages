# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

{ pkgs ? import <nixpkgs> {} }:

pkgs.runCommand "mario-1.0" { nativeBuildInputs = [ pkgs.gcc ]; } ''
  mkdir -p $out/bin
  gcc -O2 -o $out/bin/mario ${./mario.c}
''
