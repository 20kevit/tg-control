# Offline native TDLib feasibility (no Telegram access)

This is evidence of the native JSON ABI only, **not** a TDLib adapter, login, synchronization test or permission to use Telegram. The gateway still has no live adapter or runtime switch. The compliance/owner gate remains unresolved.

## Verified in this workspace

On 2026-11-06, the official `tdlib/td` source at commit `4c6377aa7386c39217878a9a56d581922ad37ca4` built `libtdjson.so.1.8.69` on Linux x86-64 with GCC 12.2, CMake 4.1.2, gperf 3.1, OpenSSL 3.0.20 and zlib 1.2.13. This limited-resource build used `-O0 -DNDEBUG` and two compiler jobs; it was not benchmarked or hardened for release. The source archive's SHA-256 was `472015605fffa826bfba6ccf7d888905b436ecce94c8a6826203792edfbe5731`. To supply missing development tools/headers in this environment, a gperf CMake mirror at commit `49da03c6ff2515be730bf39cb989379793b6dd6e` was built locally, matching OpenSSL source headers were generated, and system OpenSSL/zlib runtime libraries were linked. These are **development feasibility inputs**, not reviewed production dependencies. The build tree was about 1.5 GiB and is not committed or shipped.

`tests/tdlib_offline_probe.py` installs a Linux seccomp filter that denies socket/network and io_uring syscalls **before loading** the library, and checks that socket creation is denied. It calls the synchronous synthetic `getTextEntities`, creates two empty clients, sends only `getAuthorizationState` with distinct correlation IDs, verifies each reports `authorizationStateWaitTdlibParameters` and closes both. It never supplies TDLib parameters, API ID/hash, phone number, code, password, session, messages or real data. One receive loop handles both clients. The probe passed with the freshly built library; the full suite passed 61 tests with this optional probe enabled.

Seccomp here blocks network syscalls, not filesystem/process access or malicious native code. Run only a trusted library in an isolated environment with no real credentials. This was not a live Telegram, performance, content-protection, reauthentication or adapter test.

## Reproduce the opt-in probe

On a disposable Linux x86-64 machine with a trusted compiler, CMake, gperf, OpenSSL and zlib development files, Python dev dependencies and `libseccomp.so.2`, obtain the pinned upstream source and build outside Git (illustrative commands; inspect downloaded source and dependencies first):

```sh
mkdir -p state/tdlib-src
curl -fsSL https://codeload.github.com/tdlib/td/tar.gz/4c6377aa7386c39217878a9a56d581922ad37ca4 -o state/tdlib-src/td.tar.gz
# Verify the SHA-256 above before extraction.
tar -xzf state/tdlib-src/td.tar.gz -C state/tdlib-src
cmake -S state/tdlib-src/td-4c6377aa7386c39217878a9a56d581922ad37ca4 \
  -B state/tdlib-src/build -DCMAKE_BUILD_TYPE=Release
cmake --build state/tdlib-src/build --target tdjson --parallel 2
TG_TDLIB_LIBRARY="$PWD/state/tdlib-src/build/libtdjson.so" .venv/bin/pytest -q tests/test_native_tdlib.py
```

The test deliberately fails if its network filter cannot be installed. It is skipped when the optional library path is absent. Do not configure TDLib parameters, test real accounts, or relax the network filter. Future production work requires a legally approved gate, real adapter design and independent tests for per-account storage, authorization lifecycle, content protection/expiry, search/history completeness, and asynchronous send/update/retry behavior.
