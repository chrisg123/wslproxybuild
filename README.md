# WSL Proxy Build

Build and run .NET projects on Windows from WSL.

## Usage

From a project directory containing a `.csproj` or `.vbproj`:

```bash
python3 build.py
python3 build.py --run
python3 build.py --test
```

`--test` runs already-built tests. It does not compile first.

Test runner selection defaults to `auto`:

- SDK-style projects with `Microsoft.NET.Test.Sdk` use `dotnet test --no-build`.
- Legacy NUnit projects use `nunit3-console.exe` against the built test DLL.

Useful overrides:

```bash
python3 build.py --test --test-filter MyTestClass
python3 build.py --test --test-runner nunit
NUNIT="/mnt/c/ProgramData/chocolatey/lib/nunit-console-runner/tools/nunit3-console.exe" python3 build.py --test
```

