"""只读提取 32 位 MSVC PE 的 RTTI 类型和虚表函数地址。

用于给没有 PDB 的官方宿主建立可重复的静态调用图；不会加载目标 DLL，也不会修改
二进制。输出地址使用 PE 首选 ImageBase，动态调试时需按实际模块基址重定位。
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import pefile


def find_all(data: bytes, needle: bytes):
    start = 0
    while True:
        found = data.find(needle, start)
        if found < 0:
            return
        yield found
        start = found + 1


def analyze_type(pe: pefile.PE, data: bytes, type_name: str) -> dict[str, object]:
    base = pe.OPTIONAL_HEADER.ImageBase
    text = next(section for section in pe.sections if section.Name.startswith(b".text"))
    text_start = base + text.VirtualAddress
    text_end = text_start + max(text.Misc_VirtualSize, text.SizeOfRawData)
    name_offset = data.find(type_name.encode("ascii") + b"\0")
    result: dict[str, object] = {"type": type_name, "found": name_offset >= 0, "vtables": []}
    if name_offset < 0:
        return result

    type_descriptor_offset = name_offset - 8
    type_descriptor_va = base + pe.get_rva_from_offset(type_descriptor_offset)
    result["type_descriptor"] = hex(type_descriptor_va)

    for type_ref in find_all(data, struct.pack("<I", type_descriptor_va)):
        if type_ref < 12:
            continue
        signature, object_offset, constructor_displacement = struct.unpack_from("<III", data, type_ref - 12)
        if signature != 0 or object_offset > 0x10000 or constructor_displacement > 0x10000:
            continue
        locator_offset = type_ref - 12
        locator_va = base + pe.get_rva_from_offset(locator_offset)
        for locator_ref in find_all(data, struct.pack("<I", locator_va)):
            functions = []
            for slot in range(64):
                entry_offset = locator_ref + 4 + slot * 4
                if entry_offset + 4 > len(data):
                    break
                function_va = struct.unpack_from("<I", data, entry_offset)[0]
                if not text_start <= function_va < text_end:
                    break
                functions.append({"slot": slot, "va": hex(function_va)})
            if functions:
                vtable_va = base + pe.get_rva_from_offset(locator_ref + 4)
                result["vtables"].append(
                    {
                        "object_offset": object_offset,
                        "va": hex(vtable_va),
                        "functions": functions,
                    }
                )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pe", type=Path)
    parser.add_argument("--type", action="append", required=True, dest="types")
    args = parser.parse_args()

    data = args.pe.read_bytes()
    pe = pefile.PE(data=data, fast_load=True)
    report = {
        "file": str(args.pe.resolve()),
        "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
        "types": [analyze_type(pe, data, name) for name in args.types],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
