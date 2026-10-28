#!/usr/bin/env python3
import socket
import struct
import json
import sys

def tplink_encrypt(plaintext: str) -> bytes:
    key = 0xAB
    encrypted = bytearray()
    for b in plaintext.encode("utf-8"):
        c = key ^ b
        key = c
        encrypted.append(c)
    return bytes(encrypted)

def tplink_decrypt(ciphertext: bytes) -> str:
    key = 0xAB
    decrypted = bytearray()
    for b in ciphertext:
        p = key ^ b
        key = b
        decrypted.append(p)
    return decrypted.decode("utf-8", errors="ignore")

def tplink_send(ip: str, command: dict) -> dict:
    payload = json.dumps(command, separators=(",", ":"))
    encrypted = tplink_encrypt(payload)
    packet = struct.pack(">I", len(encrypted)) + encrypted

    with socket.create_connection((ip, 9999), timeout=5) as sock:
        sock.sendall(packet)
        response = sock.recv(8192)

    response = response[4:]
    decrypted = tplink_decrypt(response)
    return json.loads(decrypted)

def get_sysinfo(ip: str) -> dict:
    return tplink_send(ip, {"system": {"get_sysinfo": {}}})

def switch_on(ip: str) -> dict:
    return tplink_send(ip, {"system": {"set_relay_state": {"state": 1}}})

def switch_off(ip: str) -> dict:
    return tplink_send(ip, {"system": {"set_relay_state": {"state": 0}}})

def toggle(ip: str) -> dict:
    info = get_sysinfo(ip)
    current = info["system"]["get_sysinfo"]["relay_state"]
    new_state = 0 if current == 1 else 1
    return tplink_send(ip, {"system": {"set_relay_state": {"state": new_state}}})

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 tplink_control.py <IP> <MODE>")
        print("MODE: 0=OFF, 1=ON, 2=TOGGLE, 3=GET_SYSINFO")
        sys.exit(1)

    DEVICE_IP = sys.argv[1]
    MODE = int(sys.argv[2])

    if MODE == 0:
        result = switch_off(DEVICE_IP)
    elif MODE == 1:
        result = switch_on(DEVICE_IP)
    elif MODE == 2:
        result = toggle(DEVICE_IP)
    elif MODE == 3:
        result = get_sysinfo(DEVICE_IP)
    else:
        print("Invalid MODE")
        sys.exit(1)

    print(json.dumps(result, indent=2))
