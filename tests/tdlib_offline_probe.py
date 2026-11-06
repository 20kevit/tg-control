"""Minimal TDJSON ABI probe. Linux seccomp denies networking before loading TDLib.

This is a standalone subprocess used only by an explicitly opted-in feasibility test.
It never supplies TDLib parameters, API credentials, phone numbers or data.
"""

import ctypes
import errno
import json
import socket
import sys
import time


def block_network():
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("seccomp initialization failed")
    try:
        for name in (
            "socket",
            "socketpair",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "sendto",
            "sendmsg",
            "sendmmsg",
            "recvfrom",
            "recvmsg",
            "recvmmsg",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
        ):
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(
                context, 0x00050000 | errno.EPERM, number, 0
            ):
                raise RuntimeError("seccomp rule failed")
        if seccomp.seccomp_load(context):
            raise RuntimeError("seccomp activation failed")
    finally:
        seccomp.seccomp_release(context)
    try:
        socket.socket()
    except PermissionError:
        return
    raise RuntimeError("network sandbox verification failed")


def main():
    if not sys.platform.startswith("linux") or len(sys.argv) != 2:
        raise RuntimeError("Linux and one library path are required")
    block_network()  # New TDLib threads inherit this filter.
    td = ctypes.CDLL(sys.argv[1])
    td.td_create_client_id.restype = ctypes.c_int
    td.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
    td.td_receive.argtypes = [ctypes.c_double]
    td.td_receive.restype = ctypes.c_char_p
    td.td_execute.argtypes = [ctypes.c_char_p]
    td.td_execute.restype = ctypes.c_char_p
    td.td_execute(b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}')
    response = json.loads(td.td_execute(b'{"@type":"getTextEntities","text":"synthetic #test"}'))
    assert response["@type"] == "textEntities"
    clients = [td.td_create_client_id(), td.td_create_client_id()]
    assert clients[0] > 0 and clients[1] > 0 and clients[0] != clients[1]
    for index, client in enumerate(clients):
        request = {"@type": "getAuthorizationState", "@extra": f"probe-{index}"}
        td.td_send(client, json.dumps(request).encode())
    states = {}
    deadline = time.monotonic() + 15
    while len(states) < 2 and time.monotonic() < deadline:
        result = td.td_receive(0.2)
        if not result:
            continue
        message = json.loads(result)
        extra = message.get("@extra")
        client = message.get("@client_id")
        if extra in {"probe-0", "probe-1"}:
            assert client == clients[int(extra[-1])]
            states[client] = message.get("@type")
    assert set(states.values()) == {"authorizationStateWaitTdlibParameters"}
    for client in clients:
        td.td_send(client, b'{"@type":"close"}')
    closed = set()
    deadline = time.monotonic() + 10
    while len(closed) < 2 and time.monotonic() < deadline:
        result = td.td_receive(0.2)
        if result:
            message = json.loads(result)
            state = message.get("authorization_state", {}).get("@type")
            if (
                message.get("@type") == "updateAuthorizationState"
                and state == "authorizationStateClosed"
            ):
                closed.add(message.get("@client_id"))
    assert closed == set(clients)
    print("Offline TDJSON ABI and two-client correlation passed with network syscalls blocked.")


if __name__ == "__main__":
    main()
