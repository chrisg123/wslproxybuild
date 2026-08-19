#!/usr/bin/python
import sys
import stat
import re
import os
import subprocess
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from signal import signal, SIGINT

DEFAULT_DOTNET = "/mnt/c/Program Files/dotnet/dotnet.exe"
DEFAULT_NUNIT = "/mnt/c/ProgramData/chocolatey/lib/nunit-console-runner/tools/nunit3-console.exe"
DEFAULT_WINDOWS_BUILD_ROOT = "C:/Temp/wslproxybuild"
WINDOWS_BUILD_ROOT = os.getenv("WSL_PROXY_BUILD_ROOT", DEFAULT_WINDOWS_BUILD_ROOT)
NO_INCREMENTAL_BUILD = False
GENERATE_WARNIGNORE = False

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

    if args.test:
        test_output = get_run_output_path(project_file, output, args.config, args.platform, framework_ver)
        return run_tests(project_file, test_output, framework_ver, args)

    if args.run:
        run_args_from_file = get_run_args(project_file)
        run_args = args.run_args if args.run_args else run_args_from_file
        run_output = get_run_output_path(project_file, output, args.config, args.platform, framework_ver)
        exe_path = find_executable(run_output, project_file)
        run_executable(exe_path, run_args)
        return 0

    CC = None
    cmd = []

    cc_hint = ""

    vstools = to_windows_path(os.getenv('VSTOOLSPATH'))

    uses_com = project_uses_com(project_file)
    obj_path, bin_path = get_windows_build_paths()
    build_props_path = write_windows_build_props(obj_path, bin_path)
    build_targets_path = write_windows_build_targets()

    if uses_com:
        print(f"********** Project references COM assemblies **********")

    pathmap = get_pathmap(project_file)

    if framework_ver == 'net6.0' or framework_ver == 'net8.0' and not uses_com:
        cc_hint = "Ensure environment variable DOTNET is set in WSL."
        CC = get_command_path('DOTNET', DEFAULT_DOTNET)
        cmd = [
                CC,
                "restore" if args.restore else "build",
                project_file.name,
                "--verbosity",
                args.verbosity,
                "--configuration",
                args.config,
                "--framework",
                framework_ver,
                f"-p:Platform={args.platform}",
                f"-p:WarningLevel={args.warn}",
                f"-p:DirectoryBuildPropsPath={build_props_path}",
                f"-p:CustomBeforeMicrosoftCommonTargets={build_targets_path}",
            ]

        if NO_INCREMENTAL_BUILD:
            cmd.append("--no-incremental")
        if nowarn and not GENERATE_WARNIGNORE:
            cmd.append(nowarn)
        if output:
            cmd.append(f"-p:OutputPath={format_windows_output_path(output)}")
        if vstools is not None:
            cmd.append(f"-p:VSToolsPath={vstools}")
        if pathmap:
            cmd.append(f"-p:{pathmap}")
    else:
        cc_hint = "Ensure environment variable MSBUILD is set in WSL."
        CC = get_command_path('MSBUILD')
        cmd = [
                CC,
                project_file.name,
                f"/verbosity:{args.verbosity}",
                f"/p:Configuration={args.config}",
                f"/p:Platform={args.platform}",
                f"/p:WarningLevel={args.warn}",
                f"/p:DirectoryBuildPropsPath={build_props_path}",
                f"/p:CustomBeforeMicrosoftCommonTargets={build_targets_path}",
            ]

        if args.restore:
            cmd.append("/t:Restore")

        if NO_INCREMENTAL_BUILD:
            cmd.append("/t:Rebuild")
        if nowarn and not GENERATE_WARNIGNORE:
            cmd.append(nowarn)
        if output:
            cmd.append(f"/p:OutputPath={format_windows_output_path(output)}")
        if vstools is not None:
            cmd.append(f"/p:VSToolsPath={vstools}")
        if pathmap:
            cmd.append(f"/p:{pathmap}")

    if CC is None:
        sys.stderr.write(f"No compiler command.\n{cc_hint}.\n")
        return 1

    msg = f"Compiling for framework {C('green')}{framework_ver}{C('endc')}" + \
        f" with project file {C('green')}{project_file}{C('endc')}..."

    print(msg)

    working_directory = project_file.parent

    print(subprocess.list2cmdline(cmd))

    p = subprocess.Popen(
        cmd,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        encoding='utf-8',
        errors='replace')

    warning_codes = set()
    process_output(p, warning_codes if GENERATE_WARNIGNORE else None)

    if GENERATE_WARNIGNORE:
        write_warnignore(project_file, warning_codes)

    return p.returncode

def wsl_unc_to_wsl_path(p: str) -> str:
    prefix_re = re.compile(r'^\\\\(wsl\.localhost|wsl\$)\\[^\\]+\\', re.IGNORECASE)
    if prefix_re.match(p):
        rest = prefix_re.sub('', p)          # strip \\wsl.localhost\<distro>\
        rest = rest.replace('\\', '/')       # backslashes -> slashes
        return '/' + rest.lstrip('/')
    return p

def process_output(p: subprocess.Popen, warning_codes: set = None):
    while True:
        output = p.stdout.readline()

        if output == '' and p.poll() is not None:
            break

        sys.stdout.flush()
        line = output.rstrip()
        collect_warning_code(line, warning_codes)

        pattern = r'''
          (?P<full_path>
              (?:[A-Za-z]:\\[^:(]+)                 # C:\...
            | (?:\\\\(?:wsl\.localhost|wsl\$)\\[^\\]+\\[^:(]+)  # \\wsl.localhost\<distro>\...
          )
          (?:\((?P<line>\d+),(?P<col>\d+)\))?
          :\s*
          (?P<message>.*)
        '''
        m = re.search(pattern, line, re.VERBOSE)

        if m and len(m.groups()) == 4:
            full_path =  m.group('full_path')
            if full_path.startswith('\\\\'):  # UNC WSL path from Windows tools
                wsl_path = wsl_unc_to_wsl_path(full_path)
            else:  # normal C:\ path
                wsl_path = str(windows_to_wsl(PureWindowsPath(full_path)).resolve())

            line_num = m.group('line')
            col_num = m.group('col')
            msg = format_message(m.group('message'))

            if line_num and col_num:
                wsl_parsed = f"{wsl_path}:{line_num}:{col_num}"
            elif line_num:
                wsl_parsed = f"{wsl_path}:{line_num}"
            else:
                wsl_parsed = wsl_path

            print(f"{wsl_parsed}: {msg}")
            continue

        print(format_message(output), end='')

def collect_warning_code(line: str, warning_codes: set):
    if warning_codes is None:
        return

    m = re.search(r'\bwarning\s+([A-Z]+[0-9]+)\b', line, re.IGNORECASE)
    if m:
        warning_codes.add(m.group(1).upper())

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

def run_tests(
    project_file: Path,
    test_output: Path,
    framework_ver: str,
    args: argparse.Namespace,
) -> int:
    runner = get_test_runner(project_file, args)

    if runner == "nunit":
        return run_nunit_tests(project_file, test_output, args)

    return run_dotnet_tests(project_file, framework_ver, args)

def get_test_runner(project_file: Path, args: argparse.Namespace) -> str:
    if args.test_runner != "auto":
        return args.test_runner

    if project_supports_dotnet_test(project_file):
        return "dotnet"

    if project_uses_nunit(project_file):
        return "nunit"

    return "dotnet"

def run_dotnet_tests(
    project_file: Path,
    framework_ver: str,
    args: argparse.Namespace,
) -> int:
    dotnet = get_command_path('DOTNET', DEFAULT_DOTNET)
    if dotnet is None:
        sys.stderr.write("No dotnet command. Ensure environment variable DOTNET is set in WSL.\n")
        return 1

    obj_path, bin_path = get_windows_build_paths()
    build_props_path = write_windows_build_props(obj_path, bin_path)
    build_targets_path = write_windows_build_targets()
    output = get_build_output(project_file)
    vstools = to_windows_path(os.getenv('VSTOOLSPATH'))
    pathmap = get_pathmap(project_file)

    cmd = [
        dotnet,
        "test",
        project_file.name,
        "--no-build",
        "--verbosity",
        args.verbosity,
        "--configuration",
        args.config,
        f"-p:Platform={args.platform}",
        f"-p:DirectoryBuildPropsPath={build_props_path}",
        f"-p:CustomBeforeMicrosoftCommonTargets={build_targets_path}",
    ]

    if framework_ver:
        cmd.extend(["--framework", framework_ver])
    if output:
        cmd.append(f"-p:OutputPath={format_windows_output_path(output)}")
    if vstools is not None:
        cmd.append(f"-p:VSToolsPath={vstools}")
    if pathmap:
        cmd.append(f"-p:{pathmap}")
    if args.test_filter:
        cmd.extend(["--filter", f"FullyQualifiedName~{args.test_filter}"])

    print("Running tests with dotnet test...")
    print(subprocess.list2cmdline(cmd))

    return run_test_process(cmd, project_file.parent, "dotnet")

def run_nunit_tests(
    project_file: Path,
    test_output: Path,
    args: argparse.Namespace,
) -> int:
    nunit = get_command_path("NUNIT", DEFAULT_NUNIT)
    if not command_path_exists(nunit):
        sys.stderr.write(f"NUnit console runner not found: {nunit}\n")
        sys.stderr.write("Set NUNIT to the nunit3-console.exe path.\n")
        return 1

    assembly = find_assembly(test_output, project_file)
    if assembly is None:
        sys.stderr.write(f"Test assembly not found in: {test_output}\n")
        sys.stderr.write("Build the project first, then run build.py --test again.\n")
        return 1

    cmd = [
        nunit,
        "--encoding=utf-8",
        to_windows_tool_path(assembly),
    ]

    if args.test_filter:
        cmd.append(f"--test={args.test_filter}")

    print("Running tests with NUnit...")
    print(subprocess.list2cmdline(cmd))

    return run_test_process(cmd, project_file.parent, "nunit")

def run_test_process(cmd: list, working_directory: Path, formatter: str) -> int:
    p = subprocess.Popen(
        cmd,
        cwd=working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        encoding='utf-8',
        errors='replace')

    state = {}
    while True:
        output = p.stdout.readline()
        if output == '' and p.poll() is not None:
            break

        sys.stdout.flush()
        line = output.rstrip()

        stack_lines = format_test_stack_trace_line(line)
        if stack_lines:
            for stack_line in stack_lines:
                print(stack_line)
            continue

        stripped = line.strip()
        if formatter == "dotnet":
            print(format_dotnet_test_line(stripped))
        elif formatter == "nunit":
            print(format_nunit_line(stripped, state))
        else:
            print(stripped)

    p.stdout.close()
    return p.wait()

def format_test_stack_trace_line(line: str) -> list:
    m = re.search(r'( +at.*in )(C:\\[^:]+):line ([0-9]+)', line)
    if not m or len(m.groups()) != 3:
        return None

    begin = m.groups()[0]
    filename = m.groups()[1]
    linenum = int(m.groups()[2])
    drive, rest = filename.split(':', 1)
    filename = '/mnt/' + drive.lower() + rest.replace('\\', '/')
    compmodepath = f"{filename}:{linenum}:"
    return [begin, compmodepath]

def format_dotnet_test_line(line: str) -> str:
    summary = format_dotnet_summary_line(line)
    if summary:
        return summary

    m = re.match(r'(\s*Failed|\s*Passed)?\s*(\w+)\s*\[(\d+ ms)\]', line)
    if not m:
        return line

    status, test_name, duration = m.groups()
    status_text = status.strip() if status else ""
    color_status = f"{C('boldred')}{status_text}{C('endc')}" \
        if status_text == "Failed" else f"{C('green')}{status_text}{C('endc')}"
    color_test_name = f"{C('cyan')}{test_name}{C('endc')}"
    return f"{color_status} {color_test_name} [{duration}]"

def format_dotnet_summary_line(line: str) -> str:
    m = re.match(r'^(Passed!|Failed!)\s+-\s+(.+?)\s+-\s+(.+)$', line)
    if not m:
        return None

    result, stats_text, assembly = m.groups()
    pairs = re.findall(r'([^:,]+):\s*([^,]+)', stats_text)
    stats = {key.strip(): value.strip() for key, value in pairs}

    ordered_names = [
        "Total",
        "Passed",
        "Failed",
        "Skipped",
        "Warnings",
        "Inconclusive",
        "Duration",
    ]

    names = [name for name in ordered_names if name in stats]
    names.extend(name for name in stats if name not in names)

    color = C('green') if result == "Passed!" else C('boldred')
    formatted_stats = [
        format_test_summary_value(name, stats[name])
        for name in names
    ]

    return f"{color}{result}{C('endc')}  " + \
        str.join(", ", formatted_stats) + \
        f"  {assembly.strip()}"

def format_nunit_line(line: str, state: dict) -> str:
    if line in [
            "Runtime Environment",
            "Test Files",
            "Run Settings",
            "Test Run Summary",
            "Errors, Failures and Warnings"]:
        if line == "Test Run Summary":
            state["in_nunit_summary"] = True
        return f"{C('cyan')}{line}{C('endc')}"

    if re.match(r'^\d+\) Failed :', line):
        return f"{C('boldred')}{line}{C('endc')}"

    if state.get("in_nunit_summary"):
        if line == "Overall result: Passed":
            return f"{C('green')}{line}{C('endc')}"

        if line == "Overall result: Failed":
            return f"{C('boldred')}{line}{C('endc')}"

        if line.startswith("Test Count"):
            return format_nunit_count_line(line)

        if line.startswith("Duration"):
            return f"{C('blue')}{line}{C('endc')}"

    return line

def format_nunit_count_line(line: str) -> str:
    formatted = []
    for part in line.split(","):
        if ":" not in part:
            formatted.append(part.strip())
            continue

        name, value = part.split(":", 1)
        formatted.append(format_test_summary_value(name.strip(), value.strip()))

    return str.join(", ", formatted)

def format_test_summary_value(name: str, value: str) -> str:
    text = f"{name}: {value}"
    n = get_summary_number(value)

    if name in ["Total", "Test Count"] and n > 0:
        return f"{C('cyan')}{text}{C('endc')}"

    if name == "Passed" and n > 0:
        return f"{C('green')}{text}{C('endc')}"

    if name == "Failed" and n > 0:
        return f"{C('boldred')}{text}{C('endc')}"

    if name == "Warnings" and n > 0:
        return f"{C('yellow')}{text}{C('endc')}"

    if name == "Inconclusive" and n > 0:
        return f"{C('cyan')}{text}{C('endc')}"

    if name == "Skipped" and n > 0:
        return f"{C('blue')}{text}{C('endc')}"

    if name == "Duration":
        return f"{C('blue')}{text}{C('endc')}"

    return text

def get_summary_number(value: str) -> int:
    m = re.search(r'\d+', value)
    if not m:
        return 0

    return int(m.group(0))

def project_supports_dotnet_test(project_file: Path) -> bool:
    return project_has_package(project_file, "Microsoft.NET.Test.Sdk")

def project_uses_nunit(project_file: Path) -> bool:
    return project_has_package(project_file, "NUnit") or \
        "nunit.framework" in project_file.read_text(
            encoding="utf-8-sig",
            errors="replace").lower()

def project_has_package(project_file: Path, package_name: str) -> bool:
    package_name = package_name.lower()

    tree = ET.parse(project_file.resolve())
    root = tree.getroot()

    for node in root.iter():
        node_name = local_xml_name(node.tag)
        if node_name not in ["PackageReference", "Reference"]:
            continue

        include = node.attrib.get("Include", "")
        if package_name in include.lower():
            return True

    packages_file = project_file.parent / "packages.config"
    if not packages_file.exists():
        return False

    packages_root = ET.parse(packages_file.resolve()).getroot()
    for node in packages_root.iter():
        if local_xml_name(node.tag) != "package":
            continue

        package_id = node.attrib.get("id", "")
        if package_name == package_id.lower():
            return True

    return False

def local_xml_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag

def get_assembly_name(project_file: Path) -> str:
    tree = ET.parse(project_file.resolve())
    root = tree.getroot()

    for node in root.iter():
        if local_xml_name(node.tag) == "AssemblyName" and node.text:
            return node.text.strip()

    return project_file.stem

def find_assembly(search_path: Path, project: Path) -> Path:
    assembly_name = get_assembly_name(project)
    direct_path = search_path / f"{assembly_name}.dll"
    if direct_path.is_file():
        return direct_path

    if not search_path.exists():
        return None

    for p in sorted(search_path.rglob(f"{assembly_name}.dll")):
        if p.is_file():
            return p

    return None

def command_path_exists(command_path: str) -> bool:
    if command_path is None:
        return False

    if is_windows_path(command_path):
        return windows_tool_path_exists(command_path)

    if command_path.startswith("/"):
        return Path(command_path).exists()

    return True

def windows_tool_path_exists(path: str) -> bool:
    try:
        converted = subprocess.check_output(
            ["wslpath", "-u", path],
            encoding="utf-8",
            errors="replace"
        ).strip()
        return Path(converted).exists()
    except subprocess.CalledProcessError:
        return False

def to_windows_tool_path(path: Path) -> str:
    return str(PureWindowsPath(to_windows_path(str(path.resolve()))))


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
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("-r", "--run", action="store_true", help="Run")
    action_group.add_argument("-t", "--test", action="store_true", help="Run tests without building")
    action_group.add_argument("--restore", action="store_true", help="Restore NuGet packages without building")
    parser.add_argument(
        "--test-runner",
        choices=["auto", "dotnet", "nunit"],
        default=os.getenv("TEST_RUNNER", "auto").lower(),
        help="Test runner to use. Defaults to auto detection."
    )
    parser.add_argument(
        "--test-filter",
        default=os.getenv("TEST_NAME_FILTER", ""),
        help="Optional test name filter"
    )
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

def write_warnignore(project_file: Path, warning_codes: set):
    warnignore_file = project_file.parent / ".warnignore"
    existing_comments = []
    existing_warnings = set()

    if warnignore_file.exists():
        with warnignore_file.open() as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    existing_comments.append(line.rstrip("\n"))
                else:
                    existing_warnings.add(stripped.upper())

    warnings = sorted(existing_warnings | warning_codes)
    lines = []
    lines.extend(existing_comments)

    if lines and warnings and lines[-1] != "":
        lines.append("")

    lines.extend(warnings)
    warnignore_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {warnignore_file} with {len(warnings)} warning code(s).")

def get_build_output(project_file: Path) -> PureWindowsPath:
    buildoutput_file = project_file.parent / ".buildoutput"
    if buildoutput_file.exists():
        with buildoutput_file.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    return PureWindowsPath(to_windows_path(stripped))
    return None

def get_pathmap(project_file: Path) -> str:
    pathmap = os.getenv("WSL_PROXY_PATHMAP")
    if pathmap and pathmap.strip():
        return format_pathmap(pathmap.strip())

    pathmap_file = project_file.parent / ".pathmap"
    entries = []

    if pathmap_file.exists():
        with pathmap_file.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    entries.append(stripped)

    if entries:
        return format_pathmap(",".join(entries))

    return None

def format_pathmap(pathmap: str) -> str:
    if pathmap.startswith("PathMap="):
        return pathmap

    return f"PathMap={pathmap}"

def get_command_path(env_name: str, default: str = None) -> str:
    command_path = os.getenv(env_name)
    if command_path is None:
        command_path = default

    if command_path is None:
        return None

    command_path = command_path.strip()
    if not command_path:
        return None

    return command_path.strip('"').strip("'")

def to_windows_path(path: str) -> str:
    if path is None:
        return None

    path = path.strip().strip('"').strip("'")
    if not path:
        return None

    if is_windows_path(path) or not path.startswith("/"):
        return str(PureWindowsPath(path))

    converted = subprocess.check_output(
        ["wslpath", "-w", path],
        encoding="utf-8",
        errors="replace"
    ).strip()
    return str(PureWindowsPath(converted))

def is_windows_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")

def get_windows_build_root() -> PureWindowsPath:
    return PureWindowsPath(to_windows_path(WINDOWS_BUILD_ROOT))

def get_windows_build_paths() -> tuple:
    # These are evaluated from a props file so each project reference gets its own folder.
    project_name = "$(MSBuildProjectName)"
    build_root = get_windows_build_root()
    obj_path = format_windows_output_path(build_root / "obj" / project_name)
    bin_path = format_windows_output_path(build_root / "bin" / project_name)
    return obj_path, bin_path

def format_windows_output_path(output: PureWindowsPath) -> str:
    output_path = str(output)
    if output_path.endswith("\\") or output_path.endswith("/"):
        return output_path
    return output_path + "\\"

def get_run_output_path(
    project_file: Path,
    output: PureWindowsPath,
    config: str,
    platform: str,
    framework_ver: str,
) -> Path:
    if output:
        return windows_output_to_wsl_path(output)

    run_path = windows_to_wsl(get_windows_build_root() / "bin" / project_file.stem)

    if platform and platform.lower() not in ["anycpu", "any cpu"]:
        run_path = run_path / platform

    run_path = run_path / config

    if should_append_framework_to_output_path(framework_ver):
        run_path = run_path / framework_ver

    return run_path

def should_append_framework_to_output_path(framework_ver: str) -> bool:
    if not framework_ver:
        return False

    return not framework_ver.startswith("v")

def windows_output_to_wsl_path(output: PureWindowsPath) -> Path:
    if output.drive and output.drive.endswith(":"):
        return windows_to_wsl(output)

    return Path(output.as_posix())

def write_windows_build_props(obj_path: str, bin_path: str) -> str:
    props_path = get_windows_build_root() / "wslproxybuild.BuildPaths.props"
    props_wsl_path = windows_to_wsl(props_path)
    props_wsl_path.parent.mkdir(parents=True, exist_ok=True)
    props_wsl_path.write_text(
        "\n".join([
            "<Project>",
            "  <PropertyGroup>",
            f"    <BaseIntermediateOutputPath>{obj_path}</BaseIntermediateOutputPath>",
            f"    <BaseOutputPath>{bin_path}</BaseOutputPath>",
            "    <DefaultItemExcludes>$(DefaultItemExcludes);$(MSBuildProjectDirectory)\\obj\\**;$(MSBuildProjectDirectory)\\bin\\**</DefaultItemExcludes>",
            "  </PropertyGroup>",
            "</Project>",
            ""
        ]),
        encoding="utf-8"
    )
    return str(props_path)

def write_windows_build_targets() -> str:
    targets_path = get_windows_build_root() / "wslproxybuild.BuildPaths.targets"
    targets_wsl_path = windows_to_wsl(targets_path)
    targets_wsl_path.parent.mkdir(parents=True, exist_ok=True)
    targets_wsl_path.write_text(
        "\n".join([
            "<Project>",
            "  <PropertyGroup>",
            "    <_WslProxyBuildPlatformOutputPath Condition=\"'$(PlatformName)' != '' and '$(PlatformName)' != 'AnyCPU'\">$(PlatformName)\\</_WslProxyBuildPlatformOutputPath>",
            "    <OutputPath>$(BaseOutputPath)$(_WslProxyBuildPlatformOutputPath)$(Configuration)\\</OutputPath>",
            "    <OutputPath Condition=\"'$(TargetFramework)' != ''\">$(OutputPath)$(TargetFramework)\\</OutputPath>",
            "  </PropertyGroup>",
            "</Project>",
            ""
        ]),
        encoding="utf-8"
    )
    return str(targets_path)

def get_run_args(project_file: Path) -> list:
    runargs_file = project_file.parent / ".runargs"
    run_args = []

    if runargs_file.exists():
        with runargs_file.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    run_args.extend(stripped.split())

    return run_args

def windows_to_wsl(win_path: PureWindowsPath) -> Path:
    drive = win_path.drive.rstrip(':').lower()
    remainder = win_path.relative_to(win_path.anchor)
    posix_remainder = remainder.as_posix()
    return Path(f"/mnt/{drive}/{posix_remainder}")

def run_executable(exe_path: str, args: [str]):
    if not exe_path:
        print("Executable not found.")
        sys.exit(1)

    ensure_executable(exe_path)

    try:
        cmd = [exe_path] + (args if args else [])
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
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
    direct_path = search_path / f"{project.stem}.exe"
    if direct_path.is_file():
        return direct_path

    if not search_path.exists():
        return None

    for p in sorted(search_path.rglob(f"{project.stem}.exe")):
        if p.is_file():
            return p

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

def project_uses_com(project_file: Path) -> bool:
    tree = ET.parse(project_file.resolve())
    root = tree.getroot()

    if root.find(".//COMReference") is not None:
        return True

    return False

def ensure_executable(path: Path):
    if not path.exists():
        raise FileNotFoundError(path)

    if os.access(path, os.X_OK):
        return

    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"Added execute permission to {path}")
    except Exception as e:
        raise RuntimeError(
            f"{path} is not executable and permissions could not be updated: {e}"
        )

if __name__ == "__main__":
    sys.exit(main())
