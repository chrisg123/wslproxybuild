#!/usr/bin/python
import sys
import re
import os
import subprocess
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
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

    if args.run:
        exe_path = find_executable(Path(output.as_posix()), project_file)
        run_executable(exe_path, args.run_args)
        return 0

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
                f"/p:OutputPath='{output}'"
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
                f"/p:OutputPath='{output}'",
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
        errors='replace')

    process_output(p)

    return 0

def process_output(p: subprocess.Popen):
    while True:
        output = p.stdout.readline()

        if output == '' and p.poll() is not None:
            break

        sys.stdout.flush()
        line = output.rstrip()

        pattern = r'''
          (?P<full_path>                      # start capturing the path
              [A-Za-z]:\\                     #   drive letter + “:\”
              [^:(]+                          #   one or more chars that are NOT “:” or “(”
          )
          (?:\((?P<line>\d+),(?P<col>\d+)\))? # optional “(line,col)”
          :\s*                                # then a “:” and any spaces
          (?P<message>.*)                     # finally, the rest of the error message
        '''
        m = re.search(pattern, line, re.VERBOSE)

        if m and len(m.groups()) == 4:
            full_path =  m.group('full_path')
            line_num = m.group('line')
            col_num = m.group('col')
            msg = format_message(m.group('message'))

            wsl_path = windows_to_wsl(PureWindowsPath(full_path)).resolve()

            if line_num and col_num:
                wsl_parsed = f"{wsl_path}:{line_num}:{col_num}"
            elif line_num:
                wsl_parsed = f"{wsl_path}:{line_num}"
            else:
                wsl_parsed = wsl_path

            print(f"{wsl_parsed}: {msg}")
            continue

        print(format_message(output), end='')

def format_message(msg: str) -> str:
    if "Build succeeded." in msg:
        msg = msg.replace("Build succeeded.", f"{C('green')}Build succeeded.{C('endc')}")
    if "Warning(s)" in msg:
        msg = msg.replace("Warning(s)", f"{C('yellow')}Warning(s){C('endc')}")
    if "Error(s)" in msg:
        msg = msg.replace("Error(s)", f"{C('boldred')}Error(s){C('endc')}")

    formatted_msg = re.sub(r'\berror\b', f"{C('boldred')}error{C('endc')}", msg, flags=re.IGNORECASE)
    windows_path_pattern = re.compile(r'([A-Z]:\\[^\s\):]+)')

    def replace_with_wsl(match) -> str:
        win_path = match.group(1)
        return str(windows_to_wsl(PureWindowsPath(win_path)).resolve())

    formatted_msg = windows_path_pattern.sub(replace_with_wsl, formatted_msg)
    return formatted_msg


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
    parser.add_argument("-r", "--run", action="store_true", help="Run")
    parser.add_argument("--run-args", nargs=argparse.REMAINDER, help="Run arguments")
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

def get_build_output(project_file: Path, default_output: str = r"bin\Debug") -> PureWindowsPath:
    buildoutput_file = project_file.parent / ".buildoutput"
    if buildoutput_file.exists():
        with buildoutput_file.open() as f:
            content = f.read().strip()
            if content:
                return PureWindowsPath(content)
    return PureWindowsPath(default_output)

def windows_to_wsl(win_path: PureWindowsPath) -> Path:
    drive = win_path.drive.rstrip(':').lower()
    remainder = win_path.relative_to(win_path.anchor)
    posix_remainder = remainder.as_posix()
    return Path(f"/mnt/{drive}/{posix_remainder}")

def run_executable(exe_path: str, args: [str]):
    if not exe_path:
        print("Executable not found.")
        sys.exit(1)

    try:
        cmd = [exe_path] + (args if args else [])
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            encoding='utf-8',
            errors='replace'
        )
        while True:
            output = p.stdout.readline()
            if output == '' and p.poll() is not None:
                break
            print(output, end='')

    except subprocess.CalledProcessError as e:
        print(f"Execution failed with return code {e.returncode}")
    except KeyboardInterrupt:
        print("Execution interrupted.")


def find_executable(search_path: Path, project: Path) -> Path:
    exe = search_path.glob(f"{project.stem}.exe")
    result = next(exe, None)
    if result and result.is_file():
        return result
    else:
        return None

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
