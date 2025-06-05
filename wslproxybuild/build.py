#!/usr/bin/python
import sys
import re
import os
import subprocess
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from signal import signal, SIGINT

def sigint(
    _signum,
    _stackframe,
):
    sys.stdout.write("\nexit\n")
    sys.exit(0)

def main():
    signal(SIGINT, sigint)
    args = get_args()
    project_file = find_project_file()

    if project_file is None:
        sys.stderr.write("No project file found.\n")
        return 1

    framework_ver = get_framework_version(project_file)

    if framework_ver is None:
        sys.stderr.write("Could not determine framework version.\n")
        return 1

    warnings_list = get_warnignore(project_file)

    nowarn = ""
    if warnings_list:
        nowarn = "-noWarn:" + str.join(',', warnings_list)

    output = get_build_output(project_file)

    CC = None
    CFLAGS = ""

    cc_hint = ""

    vstools = os.getenv('VSTOOLSPATH')

    if framework_ver == 'net6.0':
        cc_hint = "Ensure environment variable DOTNET is set in WSL."
        CC = os.getenv('DOTNET')
        CFLAGS =  str.join(
            ' ',
            [
                "build",
                f"'{project_file.name}'",
                f"--verbosity \"{args.verbosity}\"",
                f"--configuration \"{args.config}\"",
                f"--framework \"{framework_ver}\"",
                nowarn,
                f"/p:Platform=\"{args.platform}\"",
                f"/p:WarningLevel=\"{args.warn}\"",
                f"/p:VSToolsPath='{vstools}'" if vstools != None else "",
                f"/p:OutputPath=\"{output}\"",
                "2>&1",
                "|",
                "tee"
            ]
        )
    else:
        cc_hint = "Ensure environment variable MSBUILD is set in WSL."
        CC = os.getenv('MSBUILD')
        CFLAGS = str.join(
            ' ',
            [
                nowarn,
                f"'{project_file.name}'",
                f"/verbosity:\"{args.verbosity}\"",
                f"/p:Configuration=\"{args.config}\"",
                f"/p:Platform=\"{args.platform}\"",
                f"/p:WarningLevel=\"{args.warn}\"",
                f"/p:VSToolsPath='{vstools}'" if vstools != None else "",
                f"/p:OutputPath=\"{output}\"",
                "2>&1",
                "|",
                "tee"
            ]
        )

    if CC is None:
        sys.stderr.write(f"No compiler command.\n{cc_hint}.\n")
        return 1

    msg = f"Compiling for framework {C('green')}{framework_ver}{C('endc')}" + \
        f" with project file {C('green')}{project_file}{C('endc')}..."

    print(msg)

    cmd = f"'{CC}' {CFLAGS}"

    print(cmd)

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=True,
        encoding='utf-8',
        errors='replace'
    )

    lines = []
    formatted_lines = []
    line = ""
    state = 0

    while True:
        output = p.stdout.readline()

        if output == '' and p.poll() is not None:
            break

        sys.stdout.flush()
        line = output.rstrip()
        lines.append(line)
        formatted = line
        stripped = formatted.strip()

        m = re.search(r'(?P<full_path>[A-Z]:\\[^\s\(:]*)(?:\((?P<line>\d+),(?P<col>\d+)\))?:\s*(?P<message>.*)', formatted)

        if m and len(m.groups()) == 4:
            full_path =  m.group('full_path')
            line_num = m.group('line')
            col_num = m.group('col')
            msg = format_message(m.group('message'))

            wsl_path = windows_to_wsl(full_path)

            if line_num and col_num:
                wsl_parsed = f"{wsl_path}:{line_num}:{col_num}"
            elif line_num:
                wsl_parsed = f"{wsl_path}:{line_num}"
            else:
                wsl_parsed = wsl_path

            print(f"{wsl_parsed}: {msg}")
            continue

        print(output)
    return 0

def find_project_file() -> Path:
    for p in list(Path('.').glob('*')):
        if p.suffix == '.csproj' or p.suffix == '.vbproj':
            return p
    return None

def get_framework_version(project_file: Path) -> str:
    tree = ET.parse(project_file.resolve())
    root = tree.getroot()

    ns = {'msbuild': 'http://schemas.microsoft.com/developer/msbuild/2003'}

    candidates = [
        ('.//msbuild:TargetFrameworkVersion', ns),
        ('.//TargetFrameworkVersion', {}),
        ('.//TargetFramework', {})
    ]

    for xpath, namespace in candidates:
        node = root.find(xpath, namespace)
        if node is not None and node.text:
            return node.text.strip()
    return None

def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WSL Proxy Build")
    parser.add_argument("--verbosity", default="minimal", help="Verbosity level for the build")
    parser.add_argument("--config", default="Debug", help="Build configuration")
    parser.add_argument("--platform", default="AnyCPU", help="Target platform")
    parser.add_argument("--warn", default="2", help="Warning level")
    return parser.parse_args()

def get_warnignore(project_file: Path) -> list:
    warnignore_file = project_file.parent / ".warnignore"
    warnings = []
    if warnignore_file.exists():
        with warnignore_file.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    warnings.append(stripped)
    return warnings

def get_build_output(project_file: Path, default_output: str = r"bin\Debug") -> str:
    buildoutput_file = project_file.parent / ".buildoutput"
    if buildoutput_file.exists():
        with buildoutput_file.open() as f:
            content = f.read().strip()
            if content:
                return content
    return default_output

def windows_to_wsl(win_path: str) -> str:
    drive, path = win_path.split(':', 1)
    drive = drive.lower()
    path = path.replace('\\', '/').lstrip('/')
    return f"/mnt/{drive}/{path}"

def format_message(msg: str) -> str:
    formatted_msg = re.sub(r'\berror\b', f"{C('boldred')}error{C('endc')}", msg, flags=re.IGNORECASE)
    windows_path_pattern = re.compile(r'([A-Z]:\\[^\s\):]+)')

    def replace_with_wsl(match):
        win_path = match.group(1)
        return windows_to_wsl(win_path)

    formatted_msg = windows_path_pattern.sub(replace_with_wsl, formatted_msg)
    return formatted_msg

def C(k: str) -> str:

    control_codes = {
        'endc': '\033[m',
        'red': '\033[31m',
        'boldred': '\033[1;31m',
        'green': '\033[32m',
        'yellow': '\033[33m',
        'blue': '\033[34m',
        'cyan': '\033[36m'
    }
    return control_codes[k]

if __name__ == "__main__":
    sys.exit(main())
